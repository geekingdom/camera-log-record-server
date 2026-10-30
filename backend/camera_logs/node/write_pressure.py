"""节点写入压力状态机。

写入延迟只限制新采集会话的准入，不中断已经持有连接的采集器。状态机只在跨越
准入阈值时记录事件，避免每次心跳都写入相同告警。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping

from camera_logs.common.database import now

DEFAULT_WRITE_LATENCY_LIMIT_MS = 200
MAX_WRITE_LATENCY_LIMIT_MS = 60_000
logger = logging.getLogger(__name__)


def write_latency_limit(config: Mapping[str, object] | None) -> int:
    """返回节点写入准入阈值；历史登记缺字段时保持原200毫秒行为。"""
    value = (config or {}).get("writeLatencyLimitMs", DEFAULT_WRITE_LATENCY_LIMIT_MS)
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_WRITE_LATENCY_LIMIT_MS:
        return value
    return DEFAULT_WRITE_LATENCY_LIMIT_MS


def write_latency_blocked(node: Mapping[str, object], config: Mapping[str, object] | None = None) -> bool:
    """仅当心跳使用当前阈值时采纳 Worker 结论；配置变更后立即按最坏会话复核。"""
    limit = write_latency_limit(node if config is None else config)
    admission = node.get("writeAdmission")
    if isinstance(admission, Mapping) and isinstance(admission.get("blocked"), bool) \
            and isinstance(admission.get("limitMs"), int) and not isinstance(admission.get("limitMs"), bool) \
            and admission["limitMs"] == limit:
        return admission["blocked"]
    value = node.get("writeLatencyMs", 0)
    try:
        latency = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(latency) or latency < 0:
        return False
    return latency > limit


def write_admission(metrics: Mapping[str, float | int], diagnostic: Mapping[str, object],
                    config: Mapping[str, object] | None = None) -> dict[str, object]:
    """少数慢会话有真实余量时仅告警；缺少证据、多路变慢或全局背压一律保守阻断。"""
    limit = write_latency_limit(config)
    latency = float(metrics.get("writeLatencyMs", 0))
    sessions, executor, buffer = diagnostic.get("sessions"), diagnostic.get("writeExecutor"), diagnostic.get("buffer")
    if not isinstance(sessions, Mapping) or not isinstance(executor, Mapping) or not isinstance(buffer, Mapping):
        return {"blocked": latency > limit, "reason": "LEGACY_WORST_LATENCY", "scope": "NODE", "limitMs": limit}
    slow, total, normal = (_nonnegative_int(sessions.get(name)) for name in ("slowSessions", "activeSessions", "normalSessions"))
    concurrency, active, waiting = _positive_int(executor.get("concurrency")), _nonnegative_int(executor.get("active")), _nonnegative_int(executor.get("waiting"))
    reserved, limit_bytes = _nonnegative_number(buffer.get("reservedBytes")), _positive_int(buffer.get("limitBytes"))
    if latency <= limit:
        return {"blocked": False, "reason": "WITHIN_LIMIT", "scope": "NODE", "limitMs": limit}
    if None in {slow, total, normal, concurrency, active, waiting, reserved, limit_bytes}:
        return {"blocked": True, "reason": "INCOMPLETE_DIAGNOSTICS", "scope": "NODE", "limitMs": limit}
    if reserved >= limit_bytes * .9:
        reason = "BUFFER_BACKPRESSURE"
    elif active >= concurrency or waiting > 0:
        reason = "EXECUTOR_SATURATED"
    elif total <= 1:
        reason = "SINGLE_SESSION"
    elif slow > max(1, concurrency // 4):
        reason = "TOO_MANY_SLOW_SESSIONS"
    elif slow * 2 >= total or normal < 1:
        reason = "MULTI_SESSION_PRESSURE"
    else:
        return {"blocked": False, "reason": "ISOLATED_SLOW_SESSIONS", "scope": "SESSION", "limitMs": limit,
                "slowSessions": slow, "normalSessions": normal, "activeSessions": total}
    return {"blocked": True, "reason": reason, "scope": "NODE", "limitMs": limit,
            "slowSessions": slow, "normalSessions": normal, "activeSessions": total}


def _nonnegative_number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _nonnegative_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _positive_int(value: object) -> int | None:
    parsed = _nonnegative_int(value)
    return parsed if parsed and parsed > 0 else None


class WritePressure:
    """按节点维护写入准入状态，并在状态转换时持久化审计事件。"""

    def __init__(self) -> None:
        self.level = "NORMAL"

    async def report(self, db, node_id: str, metrics: Mapping[str, float | int], config: Mapping[str, object] | None = None,
                     admission: Mapping[str, object] | None = None) -> None:
        """仅在写入延迟跨过准入阈值时记录状态变化和对应的采样快照。"""
        latency = float(metrics["writeLatencyMs"])
        limit = write_latency_limit(config)
        level = "NO_ADMISSION" if (admission or {}).get("blocked", latency > limit) else "NORMAL"
        if level == self.level:
            return
        await db.events.insert_one({
            "type": "WRITE_PRESSURE_CHANGED",
            "nodeId": node_id,
            "level": level,
            "previousLevel": self.level,
            "writeLatencyMs": latency,
            "writeLatencyLimitMs": limit,
            "writeLatencySamples": int(metrics["writeLatencySamples"]),
            "writeLatencyPendingMs": float(metrics["writeLatencyPendingMs"]),
            "writeLatencyWindowSeconds": int(metrics["writeLatencyWindowSeconds"]),
            "writeAdmissionReason": (admission or {}).get("reason", "LEGACY_WORST_LATENCY"),
            "createdAt": now(),
        })
        log = logger.warning if level == "NO_ADMISSION" else logger.info
        log("节点写入准入状态变化 node=%s level=%s latency_ms=%.3f limit_ms=%d", node_id, level, latency, limit)
        self.level = level
