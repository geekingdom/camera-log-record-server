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
    session_id = collector.session_id
    mounted_signature = (session_id, status, error)
    if status != "MOUNTED":
        collector.last_coredump_mounted = None
    repeated_mounted = status == "MOUNTED" and mounted_signature == getattr(collector, "last_coredump_mounted", None)
    if not repeated_mounted:
        try:
            await repo.db.events.insert_one({"type": "COREDUMP_MOUNT", "taskId": task["id"],
                "runId": task["runId"], "sessionId": session_id,
                "nodeId": repo.settings.node_id, "status": status, "error": error, "createdAt": now()})
        except Exception:  # 可选监控状态不能反向中断日志会话。
            logger.exception("coredump 事件记录失败 task=%s", task["id"])
        else:
            # 只有成功发布才允许抑制同会话的下一次正常挂载确认。
            if status == "MOUNTED":
                collector.last_coredump_mounted = mounted_signature
    try:
        await repo.db.tasks.update_one(owner_filter(task), {"$set": {
            "coredumpMountStatus": status, "coredumpMountError": error,
            "coredumpMountRunId": task["runId"], "coredumpCheckedAt": now()}})
    except Exception:  # 可选监控状态不能反向中断日志会话。
        logger.exception("coredump 任务状态记录失败 task=%s", task["id"])


def _eligible_coredump_task_query(resource_id, *, exclude_task_id=None):
    """返回可复用既有主机采集连接的资源级监控候选，串口和 SSH 从机永不参与。"""
    query = {"resourceId": resource_id, "status": "COLLECTING", "desiredState": "RUNNING",
             "resourceDeleted": {"$ne": True}, "$or": [
                 {"protocol": "TELNET_DEVICE"},
                 {"protocol": "SSH", "$or": [{"sshTarget": {"$exists": False}}, {"sshTarget": "HOST"}]},
             ]}
    if exclude_task_id is not None:
        query["id"] = {"$ne": exclude_task_id}
    return query


async def guard_coredump_monitor(repo, task, *, session_active, report_status, mount_target=None):
    """续租已启用且认证有效的资源，并返回当前会话的控制结果。

    ``None`` 表示同资源已有负责人，调用者仅等待重试而不新建 SSH/Telnet 连接。
    租约附带设备侧 NFS 来源，旧会话的收尾据此拒绝卸载后来迁移的不同来源。
    """
    if not session_active():
        return False
    current_task = await repo.db.tasks.find_one({**owner_filter(task), **_eligible_coredump_task_query(task.get("resourceId"))}, {"id": 1})
    if not current_task:
        return False
    resource_id = task.get("resourceId")
    if not resource_id:
        return False
    resource = await repo.db.resources.find_one(
        {"id": resource_id}, {"deletedAt": 1, "healthStatus": 1, "enableCoredumpMonitor": 1,
                               "coredumpLeaseTarget": 1},
    )
    if (not resource or resource.get("deletedAt") is not None or resource.get("healthStatus") != "ONLINE"
            or not resource.get("enableCoredumpMonitor", False)):
        return False
    if mount_target and not resource.get("coredumpLeaseTarget"):
        # 首次来源用缺失字段 CAS 固定；并发候选随后必须读取赢家，绝不能覆盖来源节点。
        await repo.db.resources.update_one(
            {"id": resource_id, "coredumpLeaseTarget": {"$exists": False}},
            {"$set": {"coredumpLeaseTarget": mount_target, "coredumpLeaseSourceNodeId": task.get("nodeId")}},
        )
        resource = await repo.db.resources.find_one({"id": resource_id}, {"coredumpLeaseTarget": 1}) or resource
    if mount_target and resource.get("coredumpLeaseTarget") not in {None, mount_target}:
        return None
    timestamp = now()
    lease = await repo.db.resources.find_one_and_update(
        {"id": resource_id, "deletedAt": None, "healthStatus": "ONLINE", "enableCoredumpMonitor": True, "$or": [
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


async def guard_coredump_cleanup(repo, task, *, session_active, mount_target=None):
    """只在资源已关闭且无接管者时授权卸载，避免退出负责人拆掉共享挂载。"""
    if not session_active():
        return False
    current_task = await repo.db.tasks.find_one(owner_filter(task), {"id": 1})
    if not current_task or not task.get("resourceId"):
        return False
    resource = await repo.db.resources.find_one(
        {"id": task["resourceId"]}, {"enableCoredumpMonitor": 1, "deletedAt": 1, "healthStatus": 1},
    )
    if resource is None:
        return False
    timestamp = now()
    lease_owner = {"id": task["resourceId"], "coredumpLeaseTaskId": task["id"],
                   "coredumpLeaseRunId": task["runId"], "coredumpLeaseGeneration": task.get("generation"),
                   "coredumpLeaseNodeId": task.get("nodeId"), "coredumpLeaseUntil": {"$gt": timestamp}}
    if mount_target:
        lease_owner["$or"] = [{"coredumpLeaseTarget": {"$exists": False}}, {"coredumpLeaseTarget": mount_target}]
    available = resource.get("deletedAt") is None and resource.get("healthStatus") == "ONLINE" and resource.get("enableCoredumpMonitor", False)
    successor = await repo.db.tasks.find_one(_eligible_coredump_task_query(task["resourceId"], exclude_task_id=task["id"]), {"id": 1})
    if available and successor is not None:
        # 先以 owner CAS 交棒。旧连接不得卸载，等待方会在下一次轻量竞争时续租。
        await repo.db.resources.update_one(lease_owner, {"$set": {"coredumpLeaseUntil": timestamp}})
        return False
    lease = await repo.db.resources.find_one_and_update(
        lease_owner,
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
        return await guard_coredump_cleanup(
            runtime.repo, runtime.task, session_active=session_active,
            mount_target=getattr(collector, "_coredump_target", None),
        )

    return guard


async def release_coredump_lease(repo, task):
    """只缩短仍属于本运行的资源租约，旧会话不得释放后继控制权。"""
    await repo.db.resources.update_one(
        {"id": task.get("resourceId"), "coredumpLeaseTaskId": task["id"],
         "coredumpLeaseRunId": task["runId"], "coredumpLeaseGeneration": task.get("generation"),
         "coredumpLeaseNodeId": task.get("nodeId")},
        {"$set": {"coredumpLeaseUntil": now()}},
    )
