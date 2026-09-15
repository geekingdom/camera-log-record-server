"""PSH 解密与设备调试的安全诊断信息。

诊断只保留可排障的阶段、HTTP 状态、受限业务码、白名单错误说明和耗时。挑战密文、
OAuth token、接口密钥、设备口令及解密成功正文都不能进入此模块的输出。
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

from camera_logs.common.observability import redact_text

_BASE64_LIKE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])")
_SAFE_MESSAGE_FIELDS = ("message", "error_description", "error")
_URL_TEXT = re.compile(r"https?://[^\s<>\"']+")
_CREDENTIAL_TEXT = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|client[_-]?secret|api[_-]?key|source)"
    r"([\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,}\]]+)"
)


def started_at() -> float:
    """返回单次远端步骤的单调计时起点，避免时钟调整影响耗时。"""
    return time.perf_counter()


def safe_endpoint(url: str) -> str:
    """保留协议、主机、端口和路径，丢弃查询串、片段和可能的用户信息。"""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "invalid"
        if ":" in host:
            host = f"[{host}]"
        authority = f"{host}:{parsed.port}" if parsed.port else host
        return f"{parsed.scheme}://{authority}{parsed.path or '/'}"
    except (TypeError, ValueError):
        return "invalid://endpoint"


def safe_text(value: object, secrets: Iterable[str] = ()) -> str:
    """将允许记录的错误说明裁剪并脱敏，防止回显密文、口令或令牌。"""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    for secret in sorted((item for item in secrets if isinstance(item, str) and item), key=len, reverse=True):
        for encoded in {secret, quote(secret, safe=""), quote_plus(secret)}:
            text = text.replace(encoded, "[REDACTED]")
    text = _URL_TEXT.sub(lambda match: safe_endpoint(match.group()), text)
    text = _CREDENTIAL_TEXT.sub(r"\1\2[REDACTED]", text)
    text = _BASE64_LIKE.sub("[REDACTED]", redact_text(text))
    return text[:512]


def response_details(payload: object, secrets: Iterable[str] = ()) -> dict[str, str]:
    """仅提取接口标准错误字段，不遍历或记录可能含解密结果的任意正文。"""
    if not isinstance(payload, Mapping):
        return {}
    # 错误响应也可能带有效令牌或口令；错误说明若回显这些字段仍必须替换。
    data = payload.get("data")
    returned_secrets = [payload.get("access_token"), payload.get("refresh_token"),
                        data.get("data") if isinstance(data, Mapping) else None]
    secrets = tuple(secrets) + tuple(value for value in returned_secrets if isinstance(value, str) and value)
    details: dict[str, str] = {}
    code = payload.get("code")
    if isinstance(code, (str, int)) and not isinstance(code, bool):
        details["serviceCode"] = safe_text(code, secrets)
    for key in _SAFE_MESSAGE_FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            details["message" if key == "message" else key] = safe_text(value, secrets)
    return details


def body_preview(content: bytes, content_type: str | None, secrets: Iterable[str] = ()) -> dict[str, Any]:
    """记录非JSON错误的有限脱敏预览，便于识别网关HTML且不保存完整返回。"""
    # 先在有界扫描窗口完成敏感字替换，再裁切预览，避免秘密恰好跨预览边界而残留前缀。
    preview = safe_text(content[:8192].decode("utf-8", errors="replace"), secrets)[:512]
    return {
        "responseContentType": safe_text(content_type or "unknown", secrets),
        "responseBytes": len(content),
        "responsePreview": preview,
        "responsePreviewTruncated": len(content) > 512,
    }


def diagnostic(
    stage: str,
    reason: str,
    *,
    started: float,
    endpoint: str | None = None,
    status: int | None = None,
    payload: object = None,
    error: BaseException | None = None,
    secrets: Iterable[str] = (),
) -> dict[str, Any]:
    """构造可安全写入 JSON 日志和运行事件的有限诊断字典。"""
    result: dict[str, Any] = {
        "stage": stage,
        "reason": reason,
        "elapsedMs": round((time.perf_counter() - started) * 1000, 3),
    }
    if endpoint:
        result["endpoint"] = safe_endpoint(endpoint)
    if status is not None:
        result["httpStatus"] = status
    result.update(response_details(payload, secrets))
    if error is not None:
        result["exceptionType"] = type(error).__name__
        message = safe_text(error, secrets)
        if message:
            result["exceptionMessage"] = message
        cause = error.__cause__ or error.__context__
        if cause is not None:
            result["causeType"] = type(cause).__name__
            cause_message = safe_text(cause, secrets)
            if cause_message:
                result["causeMessage"] = cause_message
    return result


def summary(details: Mapping[str, Any]) -> str:
    """生成可显示的固定格式摘要，只使用已安全化的有限字段。"""
    values = [f"阶段={details.get('stage', 'UNKNOWN')}", f"原因={details.get('reason', 'UNKNOWN')}"]
    for key, label in (("httpStatus", "HTTP"), ("serviceCode", "错误码"), ("message", "说明"),
                       ("error_description", "错误说明"), ("error", "错误"), ("exceptionType", "异常"),
                       ("exceptionMessage", "异常说明")):
        value = details.get(key)
        if value not in {None, ""}:
            values.append(f"{label}={value}")
    return "；".join(values)
