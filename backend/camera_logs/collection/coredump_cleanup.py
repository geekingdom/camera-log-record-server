"""核心转储 NFS 的会话内卸载命令与幂等清理控制器。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import shlex
import uuid
from pathlib import Path

from .contracts import Callback
from .contracts import call_callback as _call

logger = logging.getLogger(__name__)
CLEANUP_TIMEOUT_SECONDS = 10


class CoredumpCleanupSkipped(RuntimeError):
    """清理前或发送前失去本会话资源归属，禁止再写入设备。"""


def unmount_command(target: str, marker: str, *, mounts_path: str | Path = "/proc/mounts") -> str:
    """生成先确认、再卸载复核的命令；仅完整成功时拆分打印标记。"""
    if not target or any(character.isspace() for character in target):
        raise ValueError("NFS 挂载目标不能为空或包含空白字符")
    if not marker:
        raise ValueError("卸载确认标记不能为空")
    target_literal = shlex.quote(target)
    mounts_literal = shlex.quote(str(mounts_path))
    split = max(1, len(marker) // 2)
    left, right = shlex.quote(marker[:split]), shlex.quote(marker[split:])
    return f"""mount_target_present() {{
    : < {mounts_literal} || return 2
    while IFS=' ' read -r source mount_point rest; do
        if [ \"$source\" = {target_literal} ]; then
            mount_point=$(printf '%b' \"$mount_point\")
            return 0
        fi
    done < {mounts_literal}
    return 1
}}
mount_target_present
mount_status=$?
case \"$mount_status\" in
    1) printf '%s%s\\n' {left} {right} ;;
    0)
        if umount -l \"$mount_point\"; then
            mount_target_present
            [ \"$?\" -eq 1 ] && printf '%s%s\\n' {left} {right}
        fi
        ;;
esac
:"""


class CoredumpMountCleanup:
    """串行化本会话 NFS 卸载，停止与断线收尾同时触发时只发送一次命令。"""

    def __init__(self, cleanup_guard: Callback | None = None) -> None:
        self._cleanup_guard = cleanup_guard
        self._target: str | None = None
        self._mount_attempted = False
        self._finished = False
        self._lock = asyncio.Lock()

    def configure(self, target: str) -> None:
        """记录将要挂载的来源；首次尝试前可随资源赢家切换，之后保持不可变。"""
        if self._mount_attempted and self._target is not None and self._target != target:
            raise RuntimeError("核心转储清理目标不可在同一会话中变更")
        self._target = target

    def mark_mount_attempted(self) -> None:
        """在发送首次 NFS 配置前记录尝试，失败配置也应在会话结束时尽力回收。"""
        if self._target is None:
            raise RuntimeError("核心转储清理目标尚未配置")
        self._mount_attempted = True

    def reopen(self) -> None:
        """资源在同一采集会话内重新启用时允许下一轮挂载再次登记清理。"""
        if self._finished:
            self._finished = False
            self._mount_attempted = False

    async def run(self, send: Callback, report: Callback) -> None:
        """在仍可写当前会话时卸载目标；守卫拒绝和任何失败均不阻塞连接关闭。"""
        async with self._lock:
            if self._finished or not self._mount_attempted or self._target is None:
                return
            self._finished = True
            try:
                async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                    await self._require_cleanup_guard()
                    marker = f"coredump-unmount-{uuid.uuid4().hex}"
                    result = send(
                        unmount_command(self._target, marker), prompt=marker,
                        allow_during_shutdown=True, session_guard=self._require_cleanup_guard,
                    )
                    if inspect.isawaitable(result):
                        await result
            except asyncio.CancelledError:
                await self._report(report, "UNMOUNT_FAILED", "CancelledError")
                raise
            except CoredumpCleanupSkipped:
                await self._report(report, "UNMOUNT_SKIPPED", None)
            except Exception as error:  # noqa: BLE001 - 清理失败必须独立审计但不能阻塞关闭。
                await self._report(report, "UNMOUNT_FAILED", type(error).__name__)
            else:
                await self._report(report, "UNMOUNTED", None)

    async def unavailable(self, report: Callback) -> None:
        """接收端已到 EOF 时无法确认设备响应，记录失败且不再排队写入。"""
        async with self._lock:
            if self._finished or not self._mount_attempted or self._target is None:
                return
            self._finished = True
            await self._report(report, "UNMOUNT_FAILED", "ConnectionError")

    async def _require_cleanup_guard(self) -> None:
        """发送前复核清理归属；False 必须转换为发送器可识别的拒绝异常。"""
        if self._cleanup_guard is not None and not await _call(self._cleanup_guard):
            raise CoredumpCleanupSkipped("核心转储清理归属已失效")

    async def _report(self, report: Callback, status: str, error: str | None) -> None:
        """审计回调失败不能中断连接收尾，避免清理失败扩大为采集器泄漏。"""
        try:
            async with asyncio.timeout(3):
                await _call(report, status, error)
        except Exception:
            logger.exception("核心转储 NFS 清理状态记录失败 status=%s", status)


async def stop_monitor(collector, *, connection_usable: bool = True) -> None:
    """停止重挂载轮询后通过采集器的唯一发送队列清理本会话 NFS 目标。"""
    monitor, collector._coredump_monitor = collector._coredump_monitor, None
    if monitor and monitor is not asyncio.current_task():
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
    if collector._coredump_cleanup is None or collector._coredump_report is None:
        return
    if not connection_usable:
        await collector._coredump_cleanup.unavailable(collector._coredump_report)
        return

    async def send(command: str, **kwargs) -> None:
        await collector._enqueue(command, "\n", priority=0, timeout_seconds=10, **kwargs)

    await collector._coredump_cleanup.run(send, collector._coredump_report)
