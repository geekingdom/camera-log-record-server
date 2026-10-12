"""提供带请求关联、递归脱敏和异步轮转写入的结构化运行日志。"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import queue
import re
import sys
import time
import traceback
import uuid
import weakref
from collections import deque
from collections.abc import Mapping
from copy import copy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from starlette.datastructures import MutableHeaders
from starlette.middleware.errors import ServerErrorMiddleware
from starlette.requests import Request

from camera_logs.common.database import now
from camera_logs.common.request_context import request_context
from camera_logs.common.request_targets import request_target
from camera_logs.common.websocket_logging import track_websocket

MAX_LOG_BYTES = 20 * 1024 * 1024
MAX_EXCEPTION_TEXT_BYTES = 32 * 1024
MAX_EXCEPTION_FRAGMENT_BYTES = 4096
DEFAULT_SERVICE_LOG_RETENTION_DAYS = 7
MAX_SERVICE_LOG_RETENTION_DAYS = 3650
LOG_TIMEZONE = ZoneInfo("Asia/Shanghai")
_SECRET_KEYS = {
    "password", "token", "authorization", "passwordencrypted", "currentpassword", "newpassword",
    "adminpassword", "passwordhash", "tokenhash", "tokenencrypted", "bootstraptoken", "internaltoken",
    "encryptionkey", "cookie", "setcookie",
}
_STANDARD_RECORD_FIELDS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
_SECRET_TEXT = re.compile(
    r"(?i)\b(password[_-]?(?:encrypted|hash)|(?:current|new|admin)[_-]?password|"
    r"(?:bootstrap|internal)[_-]?token|token[_-]?(?:encrypted|hash)|encryption[_-]?key|"
    r"set[_-]?cookie|cookie|password|token|authorization)\b([\"']?\s*[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,}\]]+)"
)
_BEARER_TEXT = re.compile(r"(?i)(\bbearer\s+)[^\s,}\]\"']+")
_CURRENT_LOG_NAME = "camera-logs.jsonl"
_MANAGED_SHARD_NAME = re.compile(r"^camera-logs\.\d{4}-\d{2}-\d{2}\.\d{3,}\.jsonl$")
_LEGACY_SHARD_NAME = re.compile(r"^camera-logs\.jsonl\.\d+$")
_SERVICE_LOG_HANDLERS: weakref.WeakSet[DailySizeRotatingFileHandler] = weakref.WeakSet()


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
    def replace_secret(match: re.Match[str]) -> str:
        """保留 JSON 或 Python 字符串值的引号，确保已脱敏证据仍可被解析。"""
        secret = match.group("value")
        replacement = "[REDACTED]"
        if secret[:1] in {"'", '\"'} and secret[-1:] == secret[:1]:
            replacement = f"{secret[:1]}{replacement}{secret[:1]}"
        return f"{match.group(1)}{match.group(2)}{replacement}"
    return _SECRET_TEXT.sub(replace_secret, value)


def _take_utf8(value: str, limit: int) -> tuple[str, str]:
    """从字符串前端取不超过给定字节数的完整 UTF-8 字符，并返回剩余文本。"""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, ""
    prefix = encoded[:limit].decode("utf-8", errors="ignore")
    return prefix, value[len(prefix):]


def _bounded_exception_text(exc_info) -> str:
    """流式保留异常栈首尾，避免先拼接多 MiB 追踪再截断或阻塞日志监听器。"""
    marker = "\n[异常追踪已截断，保留首尾]\n"
    head_limit = MAX_EXCEPTION_TEXT_BYTES // 2
    tail_limit = MAX_EXCEPTION_TEXT_BYTES - head_limit - len(marker.encode("utf-8"))
    head, tail = [], deque()
    head_bytes = tail_bytes = 0
    truncated = False
    trace = traceback.TracebackException(*exc_info)
    for fragment in trace.format(chain=True):
        encoded = fragment.encode("utf-8")
        if len(encoded) > MAX_EXCEPTION_FRAGMENT_BYTES:
            # 异常参数可能直接包含设备正文或凭据，超长片段不保留任意原文。
            fragment = "[异常文本片段超过 4096 字节，已省略]\n"
            truncated = True
        else:
            fragment = redact_text(fragment)
        while fragment:
            if head_bytes < head_limit:
                prefix, fragment = _take_utf8(fragment, head_limit - head_bytes)
                head.append(prefix)
                head_bytes += len(prefix.encode("utf-8"))
                if fragment:
                    truncated = True
                    # 剩余一两个字节可能容不下一个中文字符；切入尾部预算，不能原地循环。
                    head_bytes = head_limit
                continue
            truncated = True
            prefix, fragment = _take_utf8(fragment, MAX_EXCEPTION_FRAGMENT_BYTES)
            size = len(prefix.encode("utf-8"))
            tail.append((prefix, size))
            tail_bytes += size
            while tail_bytes > tail_limit:
                _, removed = tail.popleft()
                tail_bytes -= removed
    if not truncated:
        return "".join(head)
    return "".join(head) + marker + "".join(item for item, _ in tail)


class JsonLineFormatter(logging.Formatter):
    """按事件发生时刻输出带上海时区偏移的单行脱敏JSON，不依赖容器TZ。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, LOG_TIMEZONE).isoformat(timespec="seconds"),
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
            payload["exception"] = _bounded_exception_text(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


class ExceptionPreservingQueueHandler(logging.handlers.QueueHandler):
    """保留队列入队前的异常追踪，供后台监听器完整格式化。"""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return copy(record)


def _validated_service_log_retention_days(value: object) -> int:
    """限制运行日志保留期，配置缺失或损坏时始终回退到七天。"""
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_SERVICE_LOG_RETENTION_DAYS else DEFAULT_SERVICE_LOG_RETENTION_DAYS


class DailySizeRotatingFileHandler(logging.handlers.BaseRotatingHandler):
    """按上海自然日及大小切分服务 JSONL，并只清理本处理器创建或继承的轮转文件。"""

    def __init__(self, directory: Path, *, max_bytes: int, retention_days: int):
        self.directory = directory
        self.max_bytes = max_bytes
        self.retention_days = _validated_service_log_retention_days(retention_days)
        self._cleanup_requested = False
        super().__init__(directory / _CURRENT_LOG_NAME, "a", encoding="utf-8", delay=False)
        # BaseRotatingHandler 在初始化期间才设置 baseFilename 并打开活动文件。
        self._current_day = self._existing_file_day() or self._local_now().date()
        self.cleanup_expired()

    @staticmethod
    def _local_now() -> datetime:
        """统一以服务器上海时区判断每日边界，避免容器时区改变轮转结果。"""
        return datetime.now(LOG_TIMEZONE)

    def _existing_file_day(self):
        """重启后根据活动文件最后写入日恢复边界，首条新日志即可跨日切分。"""
        try:
            return datetime.fromtimestamp(Path(self.baseFilename).stat().st_mtime, LOG_TIMEZONE).date()
        except FileNotFoundError:
            return None

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        """下一条记录跨日或超过大小上限时，先归档现有活动分片。"""
        if self._local_now().date() != self._current_day:
            return True
        if self.max_bytes <= 0:
            return False
        message = f"{self.format(record)}{self.terminator}"
        self.stream.seek(0, 2)
        return self.stream.tell() + len(message.encode(self.encoding or "utf-8")) >= self.max_bytes

    def _next_shard(self, day) -> Path:
        """同一天按递增序号命名，绝不覆盖已有运行日志。"""
        prefix = f"camera-logs.{day.isoformat()}."
        index = 1
        while True:
            candidate = self.directory / f"{prefix}{index:03d}.jsonl"
            if not candidate.exists():
                return candidate
            index += 1

    def doRollover(self) -> None:
        """原子移动活动文件后重开新片，归档失败时保留原文件并让日志框架处理错误。"""
        self.stream.close()
        source = Path(self.baseFilename)
        if source.exists() and source.stat().st_size:
            source.replace(self._next_shard(self._current_day))
        self._current_day = self._local_now().date()
        self.stream = self._open()
        self.cleanup_expired()

    def emit(self, record: logging.LogRecord) -> None:
        """在监听器线程执行文件维护，避免管理员请求在 ASGI 事件循环中进行目录扫描或删除。"""
        if self._cleanup_requested:
            self._cleanup_requested = False
            self.cleanup_expired()
        super().emit(record)

    def cleanup_expired(self, *, reference: datetime | None = None) -> None:
        """仅清理当前服务目录中已过期的受管分片；活动文件、设备正文和其它文件永不匹配。"""
        cutoff = (reference or self._local_now()).timestamp() - timedelta(days=self.retention_days).total_seconds()
        for candidate in self.directory.iterdir():
            if not (_MANAGED_SHARD_NAME.fullmatch(candidate.name) or _LEGACY_SHARD_NAME.fullmatch(candidate.name)):
                continue
            try:
                if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except FileNotFoundError:
                continue

    def set_retention_days(self, retention_days: int) -> None:
        """热更新保留期，下一条队列日志在后台监听器中复核旧分片。"""
        self.acquire()
        try:
            self.retention_days = _validated_service_log_retention_days(retention_days)
            self._cleanup_requested = True
        finally:
            self.release()


def set_service_log_retention_days(retention_days: int) -> None:
    """将管理员保存的服务日志保留期立即应用到当前进程的全部 API/Worker 处理器。"""
    for handler in list(_SERVICE_LOG_HANDLERS):
        handler.set_retention_days(retention_days)


async def apply_service_log_retention_days(retention_days: int) -> None:
    """在线程池更新处理器状态，管理员请求路径不等待任何文件锁或目录维护。"""
    await asyncio.to_thread(set_service_log_retention_days, retention_days)


async def refresh_service_log_retention(repo) -> int:
    """从共享平台配置读取保留期并应用；供 API 与每个 Worker 的生命周期轮询调用。"""
    document = await repo.db.platform_settings.find_one({"id": "platform"}) or {}
    retention_days = _validated_service_log_retention_days(document.get("serviceLogRetentionDays"))
    await apply_service_log_retention_days(retention_days)
    return retention_days


async def service_log_retention_loop(repo, *, interval_seconds: float = 60) -> None:
    """低频同步跨进程设置，读取失败不停止服务，下一轮继续尝试。"""
    while True:
        try:
            await refresh_service_log_retention(repo)
        except Exception:
            logging.getLogger(__name__).exception("服务运行日志保留期刷新失败")
        await asyncio.sleep(interval_seconds)


def setup_logging(log_dir: Path, *, retention_days: int = DEFAULT_SERVICE_LOG_RETENTION_DAYS) -> logging.handlers.QueueListener:
    """配置 stdout 与按日/20 MiB 分片的 JSONL 文件，并返回关闭时需 stop 的监听器。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = JsonLineFormatter()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    file_handler = DailySizeRotatingFileHandler(
        log_dir, max_bytes=MAX_LOG_BYTES, retention_days=retention_days,
    )
    file_handler.setFormatter(formatter)
    _SERVICE_LOG_HANDLERS.add(file_handler)
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


def _error_frames(error: BaseException) -> list[dict[str, str | int]]:
    """提取有限的结构化异常位置，保留定位能力但不记录正文、源码或局部变量。"""
    frames = traceback.extract_tb(error.__traceback__)
    return [
        {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
        for frame in frames[-16:]
    ]


def _failure_reason(request: Any, status: int, complete: bool, error: BaseException | None) -> str | None:
    """选择可持久化的失败说明，禁止将任意异常正文写入访问日志或事件。"""
    if error is not None and status < 400 and complete:
        return "响应完成后处理失败"
    configured = getattr(request.state, "safe_error", None)
    if configured:
        return redact_text(str(configured))
    if error is None:
        return None
    if status >= 500:
        return "服务内部异常"
    return "响应传输中断"


def log_request(
    request: Any,
    *,
    status: int,
    started_at: float,
    actor: str | None = None,
    error: BaseException | None = None,
    logger: logging.Logger | None = None,
    response_complete: bool = True,
    response_bytes: int = 0,
    error_reason: str | None = None,
) -> None:
    """写入不含 query、header、body 的访问日志；异常仅保留安全说明与结构化定位帧。"""
    logger = logger or logging.getLogger("camera_logs.access")
    route = getattr(getattr(request, "scope", {}), "get", lambda *_: None)("route")
    path = getattr(route, "path", None) or request.url.path
    context: dict[str, Any] = {
        "requestId": getattr(request.state, "request_id", None),
        "actor": actor if actor is not None else request_actor(request),
        "clientIp": getattr(getattr(request, "client", None), "host", None),
        "method": request.method,
        "route": path,
        **request_target(path, dict(getattr(request, "path_params", {}))),
        "targets": dict(request.path_params) if hasattr(request, "path_params") else {},
        "status": status,
        "responseComplete": response_complete,
        "responseBytes": response_bytes,
        "durationMs": round((time.perf_counter() - started_at) * 1000, 3),
    }
    if error is not None:
        context["error"] = {
            "type": type(error).__name__, "message": error_reason or "请求处理异常",
            "frames": _error_frames(error),
        }
        logger.error("request failed", extra={"context": context})
    elif response_complete:
        logger.info("request completed", extra={"context": context})
    else:
        logger.warning("request ended before response completed", extra={"context": context})


class RequestLoggingMiddleware:
    """直接观察 ASGI 发送完成，避免流式下载仅返回响应头就被记录为成功。"""

    def __init__(self, app: Any):
        self.app = app

    async def _persist_request_event(self, repo: Any, document: dict[str, Any]) -> None:
        """在很短的独立等待窗口内写入排障事件，故障不得改变原请求结果。"""
        await asyncio.wait_for(repo.db.request_events.insert_one(document), timeout=0.2)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            await track_websocket(self.app, scope, receive, send, logging.getLogger("camera_logs.access"))
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        context_token = request_context.set({"requestId": request.state.request_id,
                                             "clientIp": getattr(request.client, "host", None)})
        started_at = time.perf_counter()
        status = None
        complete = False
        sent_bytes = 0
        failure = None

        async def tracked_send(message: Any) -> None:
            nonlocal status, complete, sent_bytes
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request.state.request_id
            await send(message)
            # 仅统计发送调用已完成的数据；失败的块不能声称已传输完成。
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                sent_bytes += len(message.get("body", b""))
                complete = not message.get("more_body", False)

        try:
            await self.app(scope, receive, tracked_send)
        except (Exception, asyncio.CancelledError) as error:
            failure = error
            raise
        finally:
            fallback = 499 if isinstance(failure, asyncio.CancelledError) else 500
            final_status = status if status is not None else fallback
            reason = _failure_reason(request, final_status, complete, failure)
            try:
                log_request(request, status=final_status, started_at=started_at, error=failure,
                            response_complete=complete, response_bytes=sent_bytes, error_reason=reason)
                app = scope.get("app")
                repo = getattr(getattr(app, "state", None), "repo", None)
                path = request.url.path
                # API 请求全部留下可检索的访问事件：查询、下载、写操作和失败请求
                # 都必须能按 requestId 关联排障。仅记录路径、状态和大小，不读取正文、查询
                # 参数或请求头，避免日志事件携带设备密码、令牌及大体量日志内容。
                recordable = path.startswith("/api/v1/")
                # 取消中的协程不能再等待数据库 I/O，否则 finally 可能覆盖调用方的取消语义。
                cancelled = failure is not None and isinstance(failure, asyncio.CancelledError)
                if repo and not cancelled and path.startswith("/api/v1/") and recordable:
                    outcome = "UNKNOWN" if not complete else "FAILED" if failure is not None else (
                        "PENDING" if final_status == 202 else "FAILED" if final_status >= 400 else "SUCCEEDED")
                    level = "ERROR" if outcome == "FAILED" else "WARNING" if outcome == "UNKNOWN" else "INFO"
                    await self._persist_request_event(repo, {
                        "createdAt": now(),
                        "requestId": request.state.request_id, "actor": request_actor(request),
                        "clientIp": getattr(request.client, "host", None), "method": request.method,
                        "route": getattr(scope.get("route"), "path", None) or path, "httpStatus": final_status,
                        "outcome": outcome, "level": level, "responseComplete": complete,
                        "responseBytes": sent_bytes,
                        "durationMs": round((time.perf_counter() - started_at) * 1000, 3),
                        **request_target(getattr(scope.get("route"), "path", None) or path,
                                         dict(request.path_params)),
                        "reason": reason,
                        "errorType": type(failure).__name__ if failure is not None else None,
                        "errorFrames": _error_frames(failure) if failure is not None else None,
                    })
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 排障写入不得覆盖原请求结果或异常传播。
                logging.getLogger("camera_logs.access").warning("请求事件持久化失败", extra={"context": {
                    "errorType": type(error).__name__, "errorFrames": _error_frames(error),
                }})
            finally:
                request_context.reset(context_token)


def add_request_logging(app: Any) -> None:
    """安装内层 500 响应器与外层观察器，使未开始响应的异常也能被实际发送链路观测。"""
    handler = app.exception_handlers.get(Exception) or app.exception_handlers.get(500)
    # add_middleware 后注册者最先运行：先放置错误响应器，再放置外层观察器。
    # 已开始的流式响应由错误响应器原样重抛，绝不发送第二个 500 响应。
    app.add_middleware(ServerErrorMiddleware, handler=handler)
    app.add_middleware(RequestLoggingMiddleware)
