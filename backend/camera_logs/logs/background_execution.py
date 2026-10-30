"""归档与索引后台作业的安全身份、预算和生命周期诊断。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from camera_logs.common.observability import _error_frames

logger = logging.getLogger("camera_logs.logs.compression")
_SAFE_ARCHIVE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(slots=True)
class BackgroundJob:
    """父进程中的单槽候选；执行器仅在调度器选择后才接收该作业。"""

    kind: str
    arguments: tuple
    result: asyncio.Future
    queued_at: float
    archive_key: str = "-"
    owner: tuple[str, str, str] = ("-", "-", "-")
    token: object | None = None
    callback: Callable[[BaseException | None], int | None] | None = None


def safe_archive_owner(segments: object) -> tuple[str, str, str]:
    """从已持有的 manifest 提取唯一归属；路径、正文和秘密均不进入日志。"""
    if not isinstance(segments, (list, tuple)):
        return "-", "-", "-"
    fields = []
    for key in ("taskId", "runId", "sessionId"):
        values = {
            value if isinstance(value, str) and _SAFE_ARCHIVE_IDENTIFIER.fullmatch(value) else "-"
            for segment in segments if isinstance(segment, dict)
            for value in (segment.get(key),)
        }
        fields.append(values.pop() if len(values) == 1 else "-")
    return tuple(fields)


def archive_key(kind: str, arguments: tuple) -> str:
    """归档或索引目标的不可逆短摘要，区分同任务的不同小时且不泄露路径。"""
    source = arguments[0] if kind in {"spool", "index"} and arguments else arguments[1] if len(arguments) > 1 else "-"
    return hashlib.sha256(f"{kind}:{source}".encode()).hexdigest()[:16]


def split_background_budget(rate: int | None) -> tuple[int | None, int | None]:
    """固定将有效后台读取预算拆为归档八成和索引两成，避免并行读取超卖。"""
    if rate is None:
        return None, None
    archive_rate = max(1, rate * 4 // 5)
    return archive_rate, max(1, rate - archive_rate)


def log_lifecycle(job: BackgroundJob, phase: str, *, started_at: float | None = None,
                  error: BaseException | None = None, error_type: str | None = None,
                  retry_after_seconds: int = 0) -> None:
    """记录固定脱敏字段；失败保留有限位置帧，绝不记录异常正文或局部变量。"""
    tick = time.monotonic()
    queue_ms = max(0, round(((started_at if started_at is not None else tick) - job.queued_at) * 1000))
    run_ms = max(0, round((tick - started_at) * 1000)) if started_at is not None else 0
    task_id, run_id, session_id = job.owner
    extra: dict[str, Any] = {"context": {
        "kind": job.kind, "phase": phase, "archiveKey": job.archive_key, "taskId": task_id, "runId": run_id,
        "sessionId": session_id, "queueMs": queue_ms, "runMs": run_ms,
        "errorType": error_type or (type(error).__name__ if error is not None else None),
        "retryAfterSeconds": retry_after_seconds,
    }}
    if error is not None:
        extra["errorFrames"] = _error_frames(error)
        logger.warning("归档作业状态", extra=extra)
    else:
        logger.info("归档作业状态", extra=extra)
