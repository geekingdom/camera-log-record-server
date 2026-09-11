"""装配当前采集会话的可选资源监控，失败和等待不进入日志重连路径。"""

import asyncio
import logging

from camera_logs.collection.coredump_lease import bound_coredump_cleanup_guard, release_coredump_lease
from camera_logs.resource_metrics.runtime import monitor_loop, release_resource_monitor_lease

logger = logging.getLogger(__name__)


def _active(runtime, collector):
    """所有异步查询前后均核验原会话，防止迟到响应装配到已更换连接。"""
    return (not runtime.stopping and not runtime.retired and runtime.collector is collector
            and not collector._closed.is_set())


async def attach_coredump_monitor(runtime, collector, *, retry_seconds=5):
    """等待本地NFS或已登记来源；数据库失败仅重试装配，不访问新设备连接。"""
    missing_reported = False
    while _active(runtime, collector):
        try:
            source = await runtime.coredump_target()
            if not _active(runtime, collector):
                return
            settings = runtime.repo.settings
            if settings.nfs_server_ip or source:
                collector.start_coredump_monitor(
                    settings.nfs_server_ip, str(settings.nfs_root), runtime.on_coredump,
                    guard=runtime.coredump_guard,
                    cleanup_guard=bound_coredump_cleanup_guard(runtime, collector),
                    resolve_target=runtime.coredump_target, wait_after_false=True,
                )
                return
            if not missing_reported:
                enabled = await runtime.repo.db.resources.find_one(
                    {"id": runtime.task.get("resourceId"), "enableCoredumpMonitor": True}, {"id": 1},
                )
                if enabled and _active(runtime, collector):
                    await runtime.on_coredump("FAILED", "节点未配置 NFS_SERVER_IP，等待资源 NFS 来源")
                    missing_reported = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("可选Coredump监控装配失败，稍后重试 task=%s", runtime.task["id"])
        await asyncio.sleep(retry_seconds)


async def monitor_session(runtime, collector):
    """并行装配两种监控；取消上层会话时等待所有子协程结束，禁止遗留后台工作。"""
    children = [asyncio.create_task(attach_coredump_monitor(runtime, collector)),
                asyncio.create_task(monitor_loop(runtime, collector))]
    try:
        await asyncio.gather(*children)
    finally:
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)


async def stop_session_monitors(runtime):
    """先取消可选采样和装配，之后采集器仍可使用原连接执行最后NFS卸载。"""
    supervisor = getattr(runtime, "resource_monitor_task", None)
    runtime.resource_monitor_task = None
    if supervisor is not None:
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)


async def release_monitor_leases(runtime):
    """连接收尾后分别释放本运行租约，单个数据库异常不能跳过另一个清理。"""
    for label, release in (("资源指标", release_resource_monitor_lease), ("Coredump", release_coredump_lease)):
        try:
            await release(runtime.repo, runtime.task)
        except Exception:
            logger.exception("%s监控租约释放失败 task=%s", label, runtime.task["id"])
