"""协调同一设备资源的 Coredump 租约和独立状态记录。

租约属于资源而非采集连接。调用方仍负责判断本会话是否可用、启动设备监控及
停止采集器；本模块只用任务归属 CAS 保护资源级控制权。
"""

from datetime import timedelta

from pymongo import ReturnDocument

from camera_logs.common.database import now
from camera_logs.common.ownership import owner_filter


async def record_coredump_status(repo, task, collector, status, error, logger):
    """记录挂载事件和任务可见状态；记录失败不应中断日志接收或挂载重试。"""
    try:
        await repo.db.events.insert_one({"type": "COREDUMP_MOUNT", "taskId": task["id"],
            "runId": task["runId"], "sessionId": collector.session_id,
            "nodeId": repo.settings.node_id, "status": status, "error": error, "createdAt": now()})
        await repo.db.tasks.update_one(owner_filter(task), {"$set": {
            "coredumpMountStatus": status, "coredumpMountError": error,
            "coredumpMountRunId": task["runId"], "coredumpCheckedAt": now()}})
    except Exception:  # 可选监控状态不能反向中断日志会话。
        logger.exception("coredump 状态记录失败 task=%s", task["id"])


async def guard_coredump_monitor(repo, task, *, session_active, report_status):
    """续租健康资源并返回 True、False 或 None，分别表示可控、不可用、被占用。"""
    if not session_active():
        return False
    current_task = await repo.db.tasks.find_one(
        {**owner_filter(task), "resourceDeleted": {"$ne": True}, "desiredState": "RUNNING", "status": "COLLECTING"},
        {"id": 1},
    )
    if not current_task:
        return False
    resource_id = task.get("resourceId")
    if not resource_id:
        return False
    resource = await repo.db.resources.find_one({"id": resource_id}, {"deletedAt": 1, "healthStatus": 1})
    if not resource or resource.get("deletedAt") is not None or resource.get("healthStatus") != "ONLINE":
        await report_status("FAILED", "设备资源不可用，已停止 Coredump 监控")
        return False
    timestamp = now()
    lease = await repo.db.resources.find_one_and_update(
        {"id": resource_id, "deletedAt": None, "healthStatus": "ONLINE", "$or": [
            {"coredumpLeaseUntil": {"$exists": False}},
            {"coredumpLeaseUntil": {"$lte": timestamp}},
            {"coredumpLeaseTaskId": task["id"], "coredumpLeaseRunId": task["runId"],
             "coredumpLeaseGeneration": task.get("generation"), "coredumpLeaseNodeId": task.get("nodeId")},
        ]},
        {"$set": {"coredumpLeaseTaskId": task["id"], "coredumpLeaseRunId": task["runId"],
                  "coredumpLeaseGeneration": task.get("generation"), "coredumpLeaseNodeId": task.get("nodeId"),
                  "coredumpLeaseUntil": timestamp + timedelta(seconds=70)}},
        return_document=ReturnDocument.AFTER,
    )
    return True if lease is not None else None


async def guard_coredump_cleanup(repo, task, *, session_active):
    """仅向仍属当前会话的有效租约授权卸载，并短续租约防止收尾期间被接管。"""
    if not session_active():
        return False
    current_task = await repo.db.tasks.find_one(owner_filter(task), {"id": 1})
    if not current_task or not task.get("resourceId"):
        return False
    timestamp = now()
    lease = await repo.db.resources.find_one_and_update(
        {"id": task["resourceId"], "coredumpLeaseTaskId": task["id"],
         "coredumpLeaseRunId": task["runId"], "coredumpLeaseGeneration": task.get("generation"),
         "coredumpLeaseNodeId": task.get("nodeId"), "coredumpLeaseUntil": {"$gt": timestamp}},
        # 已有监控租约通常更长，关闭保护只能延长，不能把它意外缩短。
        {"$max": {"coredumpLeaseUntil": timestamp + timedelta(seconds=20)}},
        return_document=ReturnDocument.AFTER,
    )
    return lease is not None and session_active()


def bound_coredump_cleanup_guard(runtime, collector):
    """将关闭授权固定到实际采集器，重连后旧会话不能影响新的设备会话。"""
    async def guard():
        def session_active():
            return not (runtime.retired or runtime.collector is not collector or collector._closed.is_set())

        if not session_active():
            return False
        return await guard_coredump_cleanup(runtime.repo, runtime.task, session_active=session_active)

    return guard


async def release_coredump_lease(repo, task):
    """只缩短仍属于本运行的资源租约，旧会话不得释放后继控制权。"""
    await repo.db.resources.update_one(
        {"id": task.get("resourceId"), "coredumpLeaseTaskId": task["id"],
         "coredumpLeaseRunId": task["runId"], "coredumpLeaseGeneration": task.get("generation"),
         "coredumpLeaseNodeId": task.get("nodeId")},
        {"$set": {"coredumpLeaseUntil": now()}},
    )
