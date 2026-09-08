"""提供带请求关联、递归脱敏和异步轮转写入的结构化运行日志。"""

from __future__ import annotations

import json
import logging
import logging.handlers
import queue
import re
import sys
import time
import traceback
import uuid
from collections.abc import Mapping
from copy import copy
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware

MAX_LOG_BYTES = 20 * 1024 * 1024
LOG_BACKUP_COUNT = 14
_SECRET_KEYS = {"password", "token", "authorization", "passwordencrypted"}
_STANDARD_RECORD_FIELDS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
_SECRET_TEXT = re.compile(
    r"(?i)\b(passwordencrypted|password|token|authorization)\b(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,}\]]+)"
)
_BEARER_TEXT = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+")


def _secret_key(key: object) -> bool:
    normalized = str(key).replace("_", "").replace("-", "").lower()
    return normalized in _SECRET_KEYS


def redact(value: Any) -> Any:
    """递归脱敏可记录对象，避免密码、令牌和授权头进入 JSONL。"""
    if isinstance(value, Mapping):
        return {str(key): "[REDACTED]" if _secret_key(key) else redact(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, set):
        return [redact(item) for item in sorted(value, key=repr)]
    if isinstance(value, BaseException):
        return redact_text("".join(traceback.format_exception(value)))
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    """脱敏非结构化异常和消息中的 key=value、Bearer 文本。"""
    value = _BEARER_TEXT.sub(r"\1[REDACTED]", value)
    return _SECRET_TEXT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)


class JsonLineFormatter(logging.Formatter):
    """将日志记录格式化为单行且已脱敏的 JSON 对象。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        context = getattr(record, "context", None)
        if context is not None:
            payload["context"] = redact(context)
        extras = {
            key: value for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS and key != "context" and not key.startswith("_")
        }
        if extras:
            payload["extra"] = redact(extras)
        if record.exc_info:
            payload["exception"] = redact_text("".join(traceback.format_exception(*record.exc_info)))
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


class ExceptionPreservingQueueHandler(logging.handlers.QueueHandler):
    """保留队列入队前的异常追踪，供后台监听器完整格式化。"""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return copy(record)


def setup_logging(log_dir: Path) -> logging.handlers.QueueListener:
    """配置 stdout 与 20 MiB/14 备份 JSONL 文件，并返回需在生命周期结束时 stop 的监听器。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = JsonLineFormatter()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "camera-logs.jsonl",
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    log_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    queue_handler = ExceptionPreservingQueueHandler(log_queue)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(queue_handler)
    listener = logging.handlers.QueueListener(log_queue, stdout_handler, file_handler, respect_handler_level=True)
    listener.start()
    return listener


def request_actor(request: Any) -> str | None:
    """读取鉴权层写入的主体标识，不检查或记录原始凭据。"""
    identity = getattr(request.state, "actor", None)
    if isinstance(identity, Mapping):
        value = identity.get("id")
        return str(value) if value is not None else None
    if identity is not None:
        return str(identity)
    return None


def log_request(
    request: Any,
    *,
    status: int,
    started_at: float,
    actor: str | None = None,
    error: BaseException | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """写入不含 query、header、body 的访问日志；异常附带已脱敏追踪。"""
    logger = logger or logging.getLogger("camera_logs.access")
    route = getattr(getattr(request, "scope", {}), "get", lambda *_: None)("route")
    path = getattr(route, "path", None) or request.url.path
    context: dict[str, Any] = {
        "requestId": getattr(request.state, "request_id", None),
        "actor": actor if actor is not None else request_actor(request),
        "method": request.method,
        "route": path,
        "targets": dict(request.path_params) if hasattr(request, "path_params") else {},
        "status": status,
        "durationMs": round((time.perf_counter() - started_at) * 1000, 3),
    }
    if error is not None:
        context["error"] = {"type": type(error).__name__, "message": redact_text(str(error))}
        logger.error("request failed", extra={"context": context}, exc_info=error)
    else:
        logger.info("request completed", extra={"context": context})


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """为每个 HTTP 请求分配或沿用关联 ID，并在结束或异常时写访问日志。"""

    async def dispatch(self, request: Any, call_next: Any) -> Any:
        request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        started_at = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as error:
            log_request(request, status=500, started_at=started_at, error=error)
            raise
        response.headers["X-Request-ID"] = request.state.request_id
        log_request(request, status=response.status_code, started_at=started_at)
        return response


def add_request_logging(app: Any) -> None:
    """向 FastAPI 应用安装请求日志中间件，保持调用方无需了解实现细节。"""
    app.add_middleware(RequestLoggingMiddleware)
