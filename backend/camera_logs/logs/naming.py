"""生成可读、可跨平台使用且不含路径穿越风险的日志文件名。"""

from __future__ import annotations

import re

_UNSAFE_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]+')
TASK_NAME_MAX_UTF8_BYTES = 80


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
