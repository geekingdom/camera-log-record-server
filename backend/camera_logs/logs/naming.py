"""生成可读、可跨平台使用且不含路径穿越风险的日志文件名。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_UNSAFE_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]+')
TASK_NAME_MAX_UTF8_BYTES = 80
SHANGHAI = ZoneInfo("Asia/Shanghai")


def safe_filename_component(
    value: object,
    *,
    max_utf8_bytes: int = TASK_NAME_MAX_UTF8_BYTES,
    fallback: str = "unknown",
) -> str:
    """过滤路径和控制字符，并按 UTF-8 字节上限截断而不截断多字节字符。"""
    text = _UNSAFE_COMPONENT.sub("-", str(value).strip())
    text = re.sub(r"\s+", " ", text).strip(" .-")
    if text in {"", ".", ".."}:
        return fallback
    encoded = text.encode("utf-8")[:max_utf8_bytes]
    while encoded:
        try:
            text = encoded.decode("utf-8")
            break
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    else:
        return fallback
    return text.strip(" .-") or fallback


def safe_device_address(value: object) -> str:
    """将 IPv6 冒号替换为连字符，再使用通用组件过滤规则。"""
    return safe_filename_component(str(value).replace(":", "-"), max_utf8_bytes=64)


def _time_component(value: object) -> str | None:
    """解析带时区的服务器时间；Mongo原生无时区datetime按其UTC存储语义还原。"""
    try:
        if isinstance(value, datetime):
            instant = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        elif isinstance(value, str):
            instant = datetime.fromisoformat(value)
            if instant.tzinfo is None:
                return None
        else:
            return None
        return instant.astimezone(SHANGHAI).strftime("%Y%m%d%H%M%S")
    except (ValueError, OverflowError):
        return None


def _received_range(first_received: object, last_received: object) -> str:
    """缺少任一可靠端点时明确标注未知；不使用小时边界替代实际数据时间。"""
    first, last = _time_component(first_received), _time_component(last_received)
    return f"{first}-{last}" if first and last else "unknown-time"


def segment_filename(task_name: object, device_ip: object, first_received: object,
                     last_received: object, part: int) -> str:
    """生成秒级上海时间片段名；part保持同秒多分卷的区分，不改变持久文件身份。"""
    task = safe_filename_component(task_name)
    device = safe_device_address(device_ip)
    return f"{task}-{device}-{_received_range(first_received, last_received)}-part-{part:06d}.log"


def segment_name_from_source(source_name: str, first_received: object,
                             last_received: object) -> str:
    """保留已有任务/IP/part，仅替换时间区间；旧简名也保留分卷序号。"""
    name = Path(source_name).name
    match = re.fullmatch(r"(.+)-(?:[0-9]{14}-[0-9]{14}|unknown-time)-part-([0-9]+)\.log", name)
    if match:
        prefix, part = match.group(1), int(match.group(2))
    else:
        part_match = re.search(r"(?:^|-)part-([0-9]+)\.log$", name)
        part = int(part_match.group(1)) if part_match else 0
        prefix = name[:part_match.start()].rstrip("-") if part_match else Path(name).stem
    prefix = safe_filename_component(prefix, max_utf8_bytes=145, fallback="segment")
    return f"{prefix}-{_received_range(first_received, last_received)}-part-{part:06d}.log"
