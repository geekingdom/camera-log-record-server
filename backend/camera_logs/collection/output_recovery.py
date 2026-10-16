"""海康设备 logread 输出耗尽恢复，复用采集会话的唯一命令发送队列。

本模块先用有界的进程内冷却表消除同一 Worker 的重复告警；正常运行路径再通过
资源文档上的短期原子租约协调不同 Worker 的同一设备目标。没有资源关联的历史任务
只能使用进程内冷却，不能宣称跨节点互斥。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

from pymongo import ReturnDocument

from camera_logs.common.database import now

OUTPUT_RECOVERY_GRACE_SECONDS = 1
OUTPUT_RECOVERY_COOLDOWN_SECONDS = 60
OUTPUT_RECOVERY_LEASE_SECONDS = 60
_MAX_LOCAL_COOLDOWNS = 1024
_NOTICE = re.compile(
    rb"bspsyslogd(?:\s*:\s*the)?\s+running\s+logread\s+nums\(\d+\)\s+are\s+too\s+many"
)
_SHELL_NOISE = re.compile(
    rb"\s*(?:(?:[^\r\n]{0,80}[#$>]\s*)|(?:ls|debug|outputOpen|outputClose)\s*)"
)
_TAIL_BYTES = 160
_cooldowns: dict[str, float] = {}
logger = logging.getLogger(__name__)


class OutputRecoveryCommandSkipped(RuntimeError):
    """首条恢复命令在写入前被安全通道拒绝，采集连接仍可继续接收。"""


def _recovery_key(task: Mapping[str, Any]) -> str:
    """按实际协议目标生成本节点恢复互斥键，不混淆同资源的不同从机。"""
    resource = task.get("resourceId") or task.get("resource_id") or task.get("resource")
    return ":".join([
        "resource" if resource else "endpoint", str(resource or ""),
        str(task.get("protocol") or task.get("protocolType") or ""),
        str(task.get("ip") or task.get("host") or ""), _lease_target(task),
    ])


def _claim_recovery(key: str) -> bool:
    """在无 await 的临界区登记冷却，保证本事件循环内只允许一次恢复。"""
    now = time.monotonic()
    expired = [entry for entry, until in _cooldowns.items() if until <= now]
    for entry in expired:
        del _cooldowns[entry]
    if len(_cooldowns) >= _MAX_LOCAL_COOLDOWNS:
        # 在罕见的大量不同目标告警中优先淘汰最早到期项，限制 Worker 常驻内存。
        del _cooldowns[min(_cooldowns, key=_cooldowns.__getitem__)]
    if _cooldowns.get(key, 0) > now:
        return False
    _cooldowns[key] = now + OUTPUT_RECOVERY_COOLDOWN_SECONDS
    return True


def _lease_target(task: Mapping[str, Any]) -> str:
    """返回资源文档中的安全字段片段；主机 shell 共用租约，从机各自独立。"""
    protocol = str(task.get("protocol") or task.get("protocolType") or "")
    if protocol == "SSH":
        target = str(task.get("sshTarget") or "HOST")
        return "HOST" if target == "HOST" else f"SSH_{target}"
    return "HOST" if protocol == "TELNET_DEVICE" else protocol


async def claim_output_recovery_lease(repo: Any, task: Mapping[str, Any]) -> bool:
    """原子领取资源目标的短期恢复租约，端口变化不能绕过同设备互斥。

    未关联资源的历史任务没有跨 Worker 身份，仍由调用方的本地冷却保护。数据库
    超时或不可确认异常必须向上抛出，使会话关闭而不是冒险重复执行 kill。
    """
    resource_id = task.get("resourceId")
    if not resource_id:
        return True
    target = _lease_target(task)
    prefix = f"outputRecoveryLeases.{target}"
    timestamp = now()
    owner = {
        f"{prefix}.taskId": task.get("id"), f"{prefix}.runId": task.get("runId"),
        f"{prefix}.generation": task.get("generation"), f"{prefix}.nodeId": task.get("nodeId"),
    }
    lease = await repo.db.resources.find_one_and_update(
        {
            "id": resource_id, "deletedAt": None,
            "$or": [
                {f"{prefix}.until": {"$exists": False}},
                {f"{prefix}.until": {"$lte": timestamp}},
            ],
        },
        {"$set": owner | {f"{prefix}.until": timestamp + timedelta(seconds=OUTPUT_RECOVERY_LEASE_SECONDS)}},
        return_document=ReturnDocument.AFTER,
    )
    return lease is not None


def _has_continuing_log_output(data: bytes) -> bool:
    """判断新字节是否包含日志正文，忽略提示符、命令回显、告警和空白。"""
    remaining = _NOTICE.sub(b"", data)
    for line in remaining.splitlines() or [remaining]:
        if line.strip() and not _SHELL_NOISE.fullmatch(line):
            return True
    return False


def supports_output_recovery(task: Mapping[str, Any]) -> bool:
    """只对海康设备会话输出控制，SSH 主从都由已验证的单一会话执行。

    SSH 与设备 Telnet 都是海康网络设备的固定采集协议。串口任务可能连接海康
    设备或普通串口服务器，因此只接受运行时从绑定资源注入的明确海康标识。
    """
    protocol = task.get("protocol") or task.get("protocolType")
    if protocol in {"SSH", "TELNET_DEVICE"}:
        return True
    return protocol == "TELNET_SERIAL" and task.get("resourceKind") == "HIKVISION_NETWORK"


class OutputRecovery:
    """从唯一接收器旁路观察告警，并在持续静默时请求一次会话内恢复。"""

    def __init__(
        self,
        task: Mapping[str, Any],
        *,
        closed: Callable[[], bool],
        ready: Callable[[], bool],
        recover: Callable[[], Awaitable[None]],
        claim_lease: Callable[[], Awaitable[bool]] | None = None,
        close_on_lease_failure: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._enabled = supports_output_recovery(task)
        self._key = _recovery_key(task)
        self._closed, self._ready, self._recover = closed, ready, recover
        self._claim_lease = claim_lease
        self._close_on_lease_failure = close_on_lease_failure
        self._tail = b""
        self._pending = False
        self._attempted = False
        self._task: asyncio.Task[None] | None = None

    def observe(self, data: bytes) -> None:
        """观察原始接收字节；告警后任意新的非告警正文均视为输出已恢复。"""
        if not self._enabled or self._attempted or not data:
            return
        if self._pending and _has_continuing_log_output(data):
            self._cancel_pending()
            return
        combined = self._tail + data
        self._tail = combined[-_TAIL_BYTES:]
        if self._pending or not _NOTICE.search(combined):
            return
        self._pending = True
        self._task = asyncio.create_task(self._recover_after_quiet_window())

    async def stop(self) -> None:
        """停止或读端关闭时取消尚未执行的恢复，不在失效连接上补发输入。"""
        self._pending = False
        task, self._task = self._task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _cancel_pending(self) -> None:
        """后续正文证明输出仍持续，撤销本次恢复等待。"""
        self._pending = False
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
        self._task = None

    async def _recover_after_quiet_window(self) -> None:
        """静默窗口结束后在当前会话安全且本节点持有冷却权时恢复一次。"""
        try:
            await asyncio.sleep(OUTPUT_RECOVERY_GRACE_SECONDS)
            if not self._pending or self._closed() or not self._ready() or not _claim_recovery(self._key):
                return
            if self._claim_lease is not None and not await self._claim_lease():
                return
            self._attempted = True
            await self._recover()
        except asyncio.CancelledError:
            raise
        except OutputRecoveryCommandSkipped:
            # 发送前安全通道复核拒绝时尚未写设备，继续接收日志并等待既有会话生命周期处理。
            logger.info("logread 输出恢复因命令通道未确认而跳过 key=%s", self._key)
        except Exception:
            logger.exception("logread 输出恢复失败 key=%s", self._key)
            if self._close_on_lease_failure is not None:
                await self._close_on_lease_failure()
        finally:
            self._pending = False
            if self._task is asyncio.current_task():
                self._task = None
