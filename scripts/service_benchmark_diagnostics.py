"""保留压测失败时的阶段与发送水位，不记录异常正文、设备输出或凭据。"""

import json
import logging
from datetime import UTC, datetime


def _state(task):
    """在取消清理前获取观察器状态，不等待尚未完成的任务。"""
    if task.cancelled():
        return {"status": "CANCELLED"}
    if not task.done():
        return {"status": "PENDING"}
    error = task.exception()
    return {"status": "FAILED", "errorType": type(error).__name__} if error else {"status": "SUCCEEDED"}


def capture_failure(path, source, error, stages, elapsed, observers, background):
    """白名单记录故障现场；诊断文件写入失败不得遮盖原始压测异常。"""
    cause = getattr(source, "failure", None)
    report = {
        "passed": False,
        "time": datetime.now(UTC).isoformat(),
        "errorType": type(error).__name__,
        "causeType": type(error.__cause__).__name__ if error.__cause__ else None,
        "sourceErrorType": type(cause).__name__ if cause else None,
        "elapsedSeconds": elapsed,
        "stages": dict(stages),
        "source": {key: getattr(source, key, None) for key in (
            "route", "source_lines", "source_bytes", "connection_count",
            "elapsed_seconds", "max_tick_lag_seconds")},
        "observers": [_state(task) for task in observers],
        "background": [_state(task) for task in background],
    }
    try:
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError as diagnostic_error:
        logging.getLogger(__name__).warning("压测诊断保存失败: %s", type(diagnostic_error).__name__)
