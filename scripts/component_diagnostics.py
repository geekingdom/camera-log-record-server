"""独立部署失败的受限诊断，不依赖后端包或部署环境。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

ANSI_ESCAPE = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])")
DEFAULT_MAX_LENGTH = 1_600
URI_CREDENTIALS = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s]+@")
BEARER = re.compile(r"(?i)\bbearer\s+(?:bearer\s+)*[^\s,;\"']+")
CREDENTIAL_VALUE = re.compile(
    r"(?i)\b([\w.-]*(?:password|token|secret|key|uri)|authorization|cookie)"
    r"([\"']?[^\S\r\n]*[:=][^\S\r\n]*)[^\r\n]*"
)

# 只识别类别，不回显第三方错误原文，避免未知凭据格式绕过字段脱敏。
DIAGNOSTIC_RULES = (
    ("Registry 限流", re.compile(r"\b(?:toomanyrequests|rate limit|too many requests)\b", re.IGNORECASE)),
    ("镜像获取或 registry 认证", re.compile(
        r"\b(?:failed to pull|pull access denied|manifest unknown|image .* not found|"
        r"requested access to the resource is denied|unauthorized)\b", re.IGNORECASE)),
    ("DNS 解析", re.compile(r"\b(?:no such host|temporary failure in name resolution|lookup .+ (?:failed|:))\b", re.IGNORECASE)),
    ("连接或网络", re.compile(
        r"\b(?:connection refused|connection reset|network is unreachable|i/o timeout|"
        r"context deadline exceeded|dial tcp|timed out)\b", re.IGNORECASE)),
    ("端口占用", re.compile(r"\b(?:address already in use|port is already allocated|bind for .+ failed)\b", re.IGNORECASE)),
    ("Compose 配置", re.compile(
        r"\b(?:validating (?:compose|.*\.ya?ml)|invalid compose|services\.[\w.-]+\.|"
        r"additional property .+ is not allowed|mapping values are not allowed)\b", re.IGNORECASE)),
)


def _secret_forms(secrets: Iterable[str]) -> tuple[str, ...]:
    """生成已知临时环境值的常见传输形式，按长度倒序避免短值破坏长 URI。"""
    forms = set()
    for raw_value in secrets:
        if not isinstance(raw_value, str) or not raw_value:
            continue
        normalized = raw_value.replace("\r\n", "\n").replace("\r", "\n")
        escaped = json.dumps(normalized)[1:-1]
        for value in (raw_value, normalized, escaped, quote(normalized, safe=""), quote_plus(normalized, safe="")):
            if value:
                forms.add(value)
                forms.add(value.lower())
    return tuple(sorted(forms, key=len, reverse=True))


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """移除 ANSI 控制符并精确替换已知值，调用方必须在任何截断前调用。"""
    sanitized = ANSI_ESCAPE.sub("", text or "")
    for value in _secret_forms(secrets):
        if value:
            sanitized = re.sub(re.escape(value), "[已脱敏]", sanitized, flags=re.IGNORECASE)
    # 外部凭据可能含空格、分隔符或 Basic 方案；敏感字段后整行隐藏，不猜测值边界。
    sanitized = URI_CREDENTIALS.sub(r"\1[已脱敏]@", sanitized)
    sanitized = BEARER.sub("Bearer [已脱敏]", sanitized)
    sanitized = CREDENTIAL_VALUE.sub(r"\1\2[已脱敏]", sanitized)
    return sanitized


def _shorten(value: str, limit: int) -> str:
    """在已脱敏文本上限长，保留稳定省略标记供 CI 阅读。"""
    return value if len(value) <= limit else f"{value[:max(0, limit - 1)]}…"


def diagnose_process_failure(
    stage: str,
    returncode: int,
    stdout: str | None,
    stderr: str | None,
    *,
    secrets: Iterable[str] = (),
    max_length: int = DEFAULT_MAX_LENGTH,
) -> str:
    """返回安全且有界的失败摘要，只含固定类别，不回显第三方原文。"""
    if max_length < 80:
        raise ValueError("诊断输出上限不能小于 80 个字符")
    output = redact(f"{stdout or ''}\n{stderr or ''}", secrets)
    matched = [label for label, rule in DIAGNOSTIC_RULES if rule.search(output)]
    prefix = f"{stage}失败，退出码 {returncode}；"
    if not matched:
        return _shorten(f"{prefix}诊断类别：未分类；未匹配安全白名单诊断证据。", max_length)
    categories = "、".join(matched)
    return _shorten(f"{prefix}诊断类别：{categories}；原始输出已隐藏，请在部署主机核对对应组件。", max_length)
