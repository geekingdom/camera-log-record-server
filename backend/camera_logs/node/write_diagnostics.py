"""有界采样 Worker 事件循环与写入压力，输出可关联但不含设备正文的运行诊断。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)
SUSTAINED_SECONDS = 30
FINGERPRINT_COOLDOWN_SECONDS = 5


class EventLoopWatchdog:
    """用固定间隔采样调度超期；独立任务取消后不再保留或写入任何样本。"""

    def __init__(self, *, interval_seconds: float = .25, sample_limit: int = 240,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.sample_limit = sample_limit
        self.samples: deque[tuple[float, float]] = deque(maxlen=sample_limit)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """每个 Worker 最多运行一个 watchdog，重复调用不创建并行采样器。"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._watch(), name="worker-event-loop-watchdog")

    async def close(self) -> None:
        """关闭时等待采样协程结束，防止旧 Worker 向新循环遗留任务。"""
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def snapshot(self) -> dict[str, float | int]:
        """返回调度超期 P99；零样本明确代表 watchdog 尚未获得一次完整周期。"""
        instant = self.clock()
        while self.samples and self.samples[0][0] <= instant - 60:
            self.samples.popleft()
        values = sorted(value for _, value in self.samples)
        return {"p99Ms": values[min(len(values) - 1, int(len(values) * .99))] if values else 0.0,
                "samples": len(values), "intervalMs": self.interval_seconds * 1000}

    async def _watch(self) -> None:
        expected = self.clock() + self.interval_seconds
        while True:
            await asyncio.sleep(max(0, expected - self.clock()))
            actual = self.clock()
            self.samples.append((actual, max(0, actual - expected) * 1000))
            # 长暂停只是一条真实调度延迟；不能以零等待循环补出数十条虚假高延迟样本。
            expected = actual + self.interval_seconds


class WritePressureDiagnostics:
    """只在写压变化、持续异常或恢复时输出固定结构，避免正常批次刷运行日志。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.level = "NORMAL"
        self.fingerprint: tuple[object, ...] | None = None
        self.last_reported = 0.0

    def report(self, node_id: str, metrics: Mapping[str, float | int], limit_ms: int,
               diagnostic: Mapping[str, object], admission: Mapping[str, object] | None = None) -> str | None:
        """在既有准入阈值上记录诊断，不新增 WARNING 或改变任务接入判定。"""
        latency = float(metrics.get("writeLatencyMs", 0))
        level = "NO_ADMISSION" if (admission or {}).get("blocked", latency > limit_ms) else (
            "SESSION_WARNING" if latency > limit_ms else "NORMAL")
        task = diagnostic.get("task") if isinstance(diagnostic.get("task"), Mapping) else {}
        fingerprint = (level, task.get("taskId"), task.get("runId"), task.get("sessionId"))
        instant = self.clock()
        phase: str | None = None
        if level != self.level:
            phase = "RECOVERED" if level == "NORMAL" else "CHANGED"
        elif level != "NORMAL" and (
                instant - self.last_reported >= SUSTAINED_SECONDS
                or (fingerprint != self.fingerprint and instant - self.last_reported >= FINGERPRINT_COOLDOWN_SECONDS)):
            phase = "SUSTAINED"
        if phase is None:
            return None
        context = {
            "type": "WRITE_PRESSURE_DIAGNOSTIC", "phase": phase, "level": level,
            "nodeId": node_id, "taskId": task.get("taskId"), "runId": task.get("runId"),
            "sessionId": task.get("sessionId"), "limitMs": limit_ms,
            "admission": dict(admission or {}),
            "endToEnd": diagnostic.get("endToEnd", {}), "io": diagnostic.get("io", {}),
            "buffer": diagnostic.get("buffer", {}), "writeExecutor": diagnostic.get("writeExecutor", {}),
            "eventLoop": diagnostic.get("eventLoop", {}),
        }
        log = logger.warning if level != "NORMAL" else logger.info
        log("节点写入压力诊断", extra={"context": context})
        self.level, self.fingerprint, self.last_reported = level, fingerprint, instant
        return phase
