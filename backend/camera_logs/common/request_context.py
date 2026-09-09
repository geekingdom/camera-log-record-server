"""保存当前 HTTP 请求的非敏感关联字段，供审计写入自动复用。"""

from contextvars import ContextVar

request_context: ContextVar[dict[str, str | None] | None] = ContextVar("request_context", default=None)


def current_request_context() -> dict[str, str | None]:
    """返回当前请求关联字段的副本，后台任务和非 HTTP 调用保持为空。"""
    return dict(request_context.get() or {})
