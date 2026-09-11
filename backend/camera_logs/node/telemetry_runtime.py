"""节点遥测采样运行态：隔离线程采样、超时降级和进程关闭收尾。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from camera_logs.node.telemetry import TelemetrySampler

logger = logging.getLogger(__name__)

UNKNOWN_TELEMETRY = {"sampledAt": None, "scope": "UNKNOWN", "status": "UNKNOWN"}


class TelemetryRuntime:
    """维护单节点遥测线程，采样失败只能降低节点准入，不得中断心跳。"""

    def __init__(self, host_proc_root: Path | None) -> None:
        self.sampler = TelemetrySampler(host_proc_root)
        self.task: asyncio.Task[None] | None = None
        self.work: asyncio.Task[dict[str, Any]] | None = None
        self.value: dict[str, Any] = dict(UNKNOWN_TELEMETRY)

    def start_if_idle(self) -> None:
        """仅在线程和监督协程均结束后启动下一轮，禁止慢采样重叠。"""
        if (self.task is None or self.task.done()) and (self.work is None or self.work.done()):
            self.task = asyncio.create_task(self._sample())

    async def _sample(self) -> None:
        """有界等待线程采样；超时后固定未知结果，晚到结果不得覆盖心跳。"""
        work = self.work = asyncio.create_task(asyncio.to_thread(self.sampler.sample))
        try:
            self.value = await asyncio.wait_for(asyncio.shield(work), timeout=2)
        except TimeoutError:
            self.value = dict(UNKNOWN_TELEMETRY) | {"error": "TimeoutError"}
            logger.warning("节点遥测采样超时，指标暂记为未知", extra={"context": {
                "errorType": "TimeoutError", "timeoutSeconds": 2,
            }})
            try:
                # 超时不取消线程；关闭时同样需要等它结束，不能让 to_thread 留在进程外层。
                await asyncio.shield(work)
            except asyncio.CancelledError:
                await asyncio.shield(work)
                raise
            except Exception as error:  # noqa: BLE001 - 超时后的线程异常不得影响节点心跳。
                logger.warning("节点 telemetry 超时后采样失败 type=%s", type(error).__name__)
        except asyncio.CancelledError:
            # to_thread 无法中断；关闭者仍须等待它收尾，不能在下一周期重叠采样。
            await asyncio.shield(work)
            raise
        except Exception as error:  # noqa: BLE001 - 采样库或主机 proc 失败不能影响节点心跳。
            self.value = dict(UNKNOWN_TELEMETRY) | {"error": type(error).__name__}
            logger.warning("节点遥测采样失败，指标暂记为未知", extra={"context": {
                "errorType": type(error).__name__,
            }})

    async def close(self) -> None:
        """取消监督协程并等待线程任务，进程退出前不遗留采样工作。"""
        tasks = []
        if self.task:
            self.task.cancel()
            tasks.append(self.task)
        if self.work and self.work is not self.task:
            tasks.append(self.work)
        await asyncio.gather(*tasks, return_exceptions=True)
