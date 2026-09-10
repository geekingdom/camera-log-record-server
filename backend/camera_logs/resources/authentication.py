"""调用海康 ISAPI 认证并解析受限大小的设备只读身份信息。"""

import asyncio
from ipaddress import ip_address
from xml.etree.ElementTree import Element

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

MAX_DEVICE_INFO_BYTES = 1024 * 1024
MAX_DEVICE_FIELD_LENGTH = 512


class DeviceOfflineError(RuntimeError):
    """TCP 连接或响应超时，周期健康检查可将其显示为设备离线。"""


class DeviceAuthenticationError(RuntimeError):
    """非 401 的 HTTP、协议或设备信息错误，不能误报为离线或凭据失效。"""


def _device_info_url(ip: str) -> str:
    """构造 ISAPI 地址；IPv6 字面量必须用方括号包裹以符合 URL 语法。"""
    normalized = ip_address(ip)
    host = f"[{normalized}]" if normalized.version == 6 else str(normalized)
    return f"http://{host}/ISAPI/System/deviceInfo"


def _local_name(tag: str) -> str:
    """提取 XML 标签本地名，以兼容设备响应中的默认命名空间。"""
    return tag.rsplit("}", 1)[-1]


def _required_text(root: Element, name: str, *, allow_empty: bool = False) -> str:
    """只读取 DeviceInfo 的直接子字段，拒绝嵌套伪造字段和超长文本。"""
    node = next((child for child in root if _local_name(child.tag) == name), None)
    value = node.text.strip() if node is not None and node.text else ""
    if (not value and not allow_empty) or len(value) > MAX_DEVICE_FIELD_LENGTH:
        raise ValueError("设备信息格式无效")
    return value


def _parse_device_info(payload: bytes) -> dict[str, str]:
    """解析海康设备信息 XML，防止非法或不完整响应伪装成认证成功。"""
    try:
        root = ElementTree.fromstring(payload)
        if _local_name(root.tag) != "DeviceInfo":
            raise ValueError("设备信息格式无效")
        # 型号和序列号并非所有固件均提供，缺失不代表登录认证失败。
        model = _required_text(root, "model", allow_empty=True)
        serial = _required_text(root, "subSerialNumber", allow_empty=True)
        firmware = _required_text(root, "firmwareVersion")
        released = _required_text(root, "firmwareReleasedDate")
    except (DefusedXmlException, ElementTree.ParseError, ValueError) as exc:
        raise ValueError("设备信息格式无效") from exc
    return {"model": model, "subSerialNumber": serial, "softwareVersion": f"{firmware} {released}"}


async def authenticate_network_resource(*, ip: str, username: str, password: str, auth_type: str,
                                        transport: httpx.AsyncBaseTransport | None = None) -> dict[str, str]:
    """以指定 HTTP 认证验证设备凭据，并返回来自设备的可信身份信息。"""
    auth = httpx.DigestAuth(username, password) if auth_type == "DIGEST" else httpx.BasicAuth(username, password)
    try:
        async with asyncio.timeout(10):
            async with httpx.AsyncClient(timeout=10, transport=transport, trust_env=False) as client:
                async with client.stream("GET", _device_info_url(ip), auth=auth) as response:
                    if response.status_code == 401:
                        raise PermissionError("设备凭据错误")
                    if response.status_code != 200:
                        raise DeviceAuthenticationError("设备异常")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_DEVICE_INFO_BYTES:
                            raise ValueError("设备信息格式无效")
    except TimeoutError as exc:
        # 周期健康检查需要区分超时离线与设备返回的其它异常；API 仍统一映射为 502。
        raise DeviceOfflineError("设备异常") from exc
    except httpx.HTTPError as exc:
        raise DeviceOfflineError("设备异常") from exc
    return _parse_device_info(bytes(chunks))
