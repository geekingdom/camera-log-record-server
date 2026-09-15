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
    """超过节点有效阈值时仅拒绝新会话；无效遥测保持原有非阻断兼容语义。"""
    value = node.get("writeLatencyMs", 0)
    try:
        latency = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(latency) or latency < 0:
        return False
    return latency > write_latency_limit(node if config is None else config)


class WritePressure:
    """按节点维护写入准入状态，并在状态转换时持久化审计事件。"""

    def __init__(self) -> None:
        self.level = "NORMAL"

    async def report(self, db, node_id: str, metrics: Mapping[str, float | int], config: Mapping[str, object] | None = None) -> None:
        """仅在写入延迟跨过准入阈值时记录状态变化和对应的采样快照。"""
        latency = float(metrics["writeLatencyMs"])
        limit = write_latency_limit(config)
        level = "NO_ADMISSION" if latency > limit else "NORMAL"
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
            "createdAt": now(),
        })
        log = logger.warning if level == "NO_ADMISSION" else logger.info
        log("节点写入准入状态变化 node=%s level=%s latency_ms=%.3f limit_ms=%d", node_id, level, latency, limit)
        self.level = level
