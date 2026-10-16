"""在 API 边界把绝对时间展示为上海时区，存储层仍保留 UTC 时刻。"""

import json
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    """返回存储、TTL 与跨进程比较统一使用的 UTC aware 当前时刻。"""
    return utc_now()


def utc_now() -> datetime:
    """为 Mongo 查询、TTL 和跨进程期限比较提供规范 UTC 绝对时刻。"""
    return datetime.now(UTC)


def utc_instant(value: datetime) -> datetime:
    """把 BSON 读出的 naive 旧值按 Mongo UTC 约定补齐时区后规范化。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_iso(value: datetime) -> str:
    """把目录等需要字典序范围查询的时间键固定写为 UTC ISO 文本。"""
    return utc_instant(value).isoformat()


def _is_presentation_time_field(name: str | None) -> bool:
    """只认定稳定业务时间字段，日志正文、命令与不透明 ID 绝不按内容猜测。"""
    return bool(name and (name.endswith(("At", "Until", "Since")) or name in {
        "start", "end", "timestamp", "time", "hour", "hourStart", "heartbeat", "latestIndexedHour",
        "contiguousCoveredThrough",
    }))


def _present_scalar(value: Any, field_name: str | None) -> Any:
    """仅转换明确时间字段的带时区 ISO 文本，目录键和普通文本保持原样。"""
    if not _is_presentation_time_field(field_name) or not isinstance(value, str) or "T" not in value:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    # Mongo 驱动或旧记录读出的 naive datetime 依 BSON 约定表示 UTC，不能当成本地墙钟时间。
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(SHANGHAI).isoformat()


def present_json_time(value: Any, field_name: str | None = None) -> Any:
    """递归转换明确时间字段，避免把设备正文或外部不透明值当作日期处理。"""
    # 文档示例由reference源定义时区，不能重写请求样例、Schema或外部扩展文档。
    if field_name in {"requestExample", "responseExample", "requestSchema", "schemas", "metadata", "extensions"}:
        return value
    if isinstance(value, dict):
        return {key: present_json_time(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [present_json_time(item, field_name) for item in value]
    return _present_scalar(value, field_name)


def _is_json(headers: list[tuple[bytes, bytes]]) -> bool:
    """判断 HTTP 响应媒体类型，缺少或非 JSON 的流保持原有字节边界。"""
    for name, value in headers:
        if name.lower() == b"content-type":
            media_type = value.decode("latin-1").split(";", 1)[0].strip().lower()
            return media_type == "application/json" or media_type.endswith("+json")
    return False


def _encode_json(payload: bytes) -> bytes:
    """解码并重新编码常规 JSON 响应；非 JSON 内容由调用方原样转发。"""
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload
    return json.dumps(present_json_time(value), ensure_ascii=False, separators=(",", ":")).encode()


class PresentationTimeMiddleware:
    """拦截普通 HTTP JSON 与 WebSocket JSON 帧，在最终序列化后转换明确时间字段。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        """仅处理单块 JSON 响应；下载和多块流均保持原有字节边界与时序。"""
        if scope["type"] == "websocket":
            async def websocket_send(message):
                if message["type"] != "websocket.send":
                    await send(message)
                    return
                if message.get("text") is not None:
                    await send({**message, "text": _encode_json(message["text"].encode()).decode()})
                    return
                if message.get("bytes") is not None:
                    await send({**message, "bytes": _encode_json(message["bytes"])})
                    return
                await send(message)

            await self.app(scope, receive, websocket_send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = None
        is_json = False

        async def http_send(message):
            nonlocal start, is_json
            if message["type"] == "http.response.start":
                is_json = _is_json(message.get("headers", []))
                if not is_json:
                    await send(message)
                    return
                start = message
                return
            if message["type"] != "http.response.body" or not is_json:
                await send(message)
                return
            if message.get("more_body", False):
                # StreamingResponse 不能安全收集到内存，立即恢复其原有发送语义。
                await send(start)
                await send(message)
                start, is_json = None, False
                return
            payload = _encode_json(message.get("body", b""))
            headers = [(name, value) for name, value in start.get("headers", []) if name.lower() != b"content-length"]
            headers.append((b"content-length", str(len(payload)).encode()))
            await send({**start, "headers": headers})
            await send({**message, "body": payload, "more_body": False})

        await self.app(scope, receive, http_send)
