"""节点写入压力状态机。

写入延迟只限制新采集会话的准入，不中断已经持有连接的采集器。状态机只在跨越
准入阈值时记录事件，避免每次心跳都写入相同告警。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from camera_logs.common.database import now

WRITE_LATENCY_LIMIT_MS = 200.0
logger = logging.getLogger(__name__)


class WritePressure:
    """按节点维护写入准入状态，并在状态转换时持久化审计事件。"""

    def __init__(self) -> None:
        self.level = "NORMAL"

    async def report(self, db, node_id: str, metrics: Mapping[str, float | int]) -> None:
        """仅在写入延迟跨过准入阈值时记录状态变化和对应的采样快照。"""
        latency = float(metrics["writeLatencyMs"])
        level = "NO_ADMISSION" if latency > WRITE_LATENCY_LIMIT_MS else "NORMAL"
        if level == self.level:
            return
        await db.events.insert_one({
            "type": "WRITE_PRESSURE_CHANGED",
            "nodeId": node_id,
            "level": level,
            "previousLevel": self.level,
            "writeLatencyMs": latency,
            "writeLatencySamples": int(metrics["writeLatencySamples"]),
            "writeLatencyPendingMs": float(metrics["writeLatencyPendingMs"]),
            "writeLatencyWindowSeconds": int(metrics["writeLatencyWindowSeconds"]),
            "createdAt": now(),
        })
        log = logger.warning if level == "NO_ADMISSION" else logger.info
        log("节点写入准入状态变化 node=%s level=%s latency_ms=%.3f", node_id, level, latency)
        self.level = level
