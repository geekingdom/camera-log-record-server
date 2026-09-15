"""采集节点受控退出编排。

进程收到 SIGTERM 时，此模块只处理本机已拥有的运行实例。它先停止物理连接，
再读取持久化控制意图，避免把用户在关闭窗口内发起的暂停误写为停止。归属不明或
阻塞任务只隔离连接，绝不释放可能仍被旧实例使用的锁。
"""

import asyncio
import logging

from camera_logs.common.database import now
from camera_logs.common.ownership import owner_filter

logger = logging.getLogger(__name__)
SHUTDOWN_CONCURRENCY = 16


async def mark_unavailable_for_shutdown(worker):
    """关闭前撤销节点准入，防止旧心跳有效期内被再次分配任务。"""
    changed = await worker.repo.db.nodes.update_one(
        {"id": worker.repo.settings.node_id},
        {"$set": {"accepting": False, "shuttingDownAt": now()}},
    )
    if not changed.matched_count:
        raise RuntimeError("关闭前未找到节点心跳，不能确认已经撤销准入")


async def shutdown_runtime(worker, runtime, *, allow_release=True):
    """关闭一个运行并按关闭完成后的用户意图进行保守持久化收尾。"""
    try:
        await runtime.stop()
    except (TimeoutError, OSError, RuntimeError) as stop_error:
        # 物理关闭没有确认时，沿用标准失败路径标为 BLOCKED，不能自动接管。
        await worker.finish_runtime(runtime, "release", stop_error=stop_error)
        return
    if not allow_release:
        await worker.finish_runtime(runtime, "isolate", already_stopped=True)
        return
    try:
        current = await worker.repo.db.tasks.find_one(owner_filter(runtime.task))
    except Exception:
        logger.exception("节点关闭后无法确认任务归属，仅保留隔离结果 task=%s", runtime.task["id"])
        await worker.finish_runtime(runtime, "isolate", already_stopped=True)
        return
    if current is None or current.get("status") == "BLOCKED" or getattr(runtime, "retired", False):
        await worker.finish_runtime(runtime, "isolate", already_stopped=True)
    elif current.get("desiredState") == "PAUSED":
        await worker.finish_runtime(runtime, "pause", already_stopped=True)
    else:
        await worker.finish_runtime(runtime, "release", already_stopped=True)


async def shutdown_active_runtimes(worker, *, allow_release=True, excluded_task_ids: set[str] | None = None):
    """按固定并发批次关闭未收尾会话，已有收尾任务不可重复停止。"""
    excluded = excluded_task_ids or set()
    runtimes = [runtime for task_id, runtime in worker.active.items() if task_id not in excluded]
    for index in range(0, len(runtimes), SHUTDOWN_CONCURRENCY):
        batch = runtimes[index : index + SHUTDOWN_CONCURRENCY]
        results = await asyncio.gather(
            *(shutdown_runtime(worker, runtime, allow_release=allow_release) for runtime in batch), return_exceptions=True
        )
        for runtime, result in zip(batch, results, strict=True):
            if isinstance(result, Exception):
                logger.error("节点停止失败 task=%s error=%s", runtime.task["id"], result)


async def shutdown_runtimes(worker, *, allow_release=True):
    """并行等待既有收尾并关闭其余会话，取消时不留下脱离生命周期的关闭任务。"""
    # 已有release可能等待最终落盘，正常退出不可重复stop或强制取消它，也不能阻塞其他连接关闭。
    pending_releases = dict(worker.releases)
    close_remaining = asyncio.create_task(
        shutdown_active_runtimes(worker, allow_release=allow_release, excluded_task_ids=set(pending_releases))
    )
    results = await asyncio.gather(*pending_releases.values(), close_remaining, return_exceptions=True)
    for task_id, result in zip(pending_releases, results, strict=False):
        if isinstance(result, Exception):
            logger.error("节点既有收尾失败 task=%s error_type=%s", task_id, type(result).__name__)
    remaining = results[-1]
    if isinstance(remaining, Exception):
        logger.error("节点剩余会话收尾失败 error_type=%s", type(remaining).__name__)
