"""资源认证状态对任务准入和停止意图的统一约束。"""

from collections.abc import Callable

from pymongo import ReturnDocument

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.common.ownership import owner_filter

BAD_HEALTH_STATUSES = frozenset({"AUTH_FAILED", "OFFLINE", "ERROR"})
ACTIVE_RUNTIME_STATUSES = frozenset({"PENDING", "CONNECTING", "COLLECTING", "RECONNECTING", "PAUSING"})


def resource_health_blocks_task(task, resource) -> bool:
    """返回任务是否应受关联网络资源的认证状态阻止，串口 Telnet 独立于 HTTP 认证。"""
    return task.get("protocol") != "TELNET_SERIAL" and resource.get("healthStatus") in BAD_HEALTH_STATUSES


def has_live_runtime_ownership(task) -> bool:
    """已领取且处于 Worker 活跃状态的运行由本机接收证据裁决，健康轮询不可抢停。"""
    return task.get("nodeId") is not None and task.get("status") in ACTIVE_RUNTIME_STATUSES \
        and task.get("desiredState") == "RUNNING"


def _task_control_query(task, *, require_owner: bool) -> dict:
    """构造停止的 CAS 条件，防止过期健康快照覆盖新的控制意图或运行代次。"""
    query = {"id": task["id"], "desiredState": "RUNNING",
             "controlClaimVersion": task.get("controlClaimVersion")}
    if require_owner:
        query.update(owner_filter(task))
    else:
        query["nodeId"] = None
    return query


async def stop_for_resource_health(repo, task, resource, session, *, require_owner=False,
                                   accepted_statuses=BAD_HEALTH_STATUSES,
                                   liveness_check: Callable[[], bool] | None = None) -> bool:
    """在既有事务内因当前坏资源状态停止任务，并只在实际停止时写入操作和审计。

    周期健康检查只允许停止未领取任务；Worker 传入 ``require_owner`` 后，必须同时
    保持原 run、node、generation，才可根据本地无输出证据停止自己的运行。
    """
    if task.get("protocol") == "TELNET_SERIAL" or task.get("desiredState") != "RUNNING":
        return False
    if not require_owner and has_live_runtime_ownership(task):
        return False
    current_resource = await repo.db.resources.find_one_and_update(
        {"id": resource["id"], "deletedAt": None,
         "healthStatus": {"$in": sorted(accepted_statuses)}},
        {"$inc": {"controlClaimVersion": 1}},
        return_document=ReturnDocument.AFTER, session=session,
    )
    if current_resource is None:
        return False
    # 资源读写等待期间会话可能已经重新收到日志；由 Worker 提供的单调时钟判据必须
    # 在写入停止意图前重新成立，事务重试也会重新执行该回调。
    if liveness_check is not None and not liveness_check():
        return False
    query = _task_control_query(task, require_owner=require_owner)
    timestamp = now()
    # 401 锁存跨后续网络错误保留；停止标记必须保留其恢复前提，不能被 OFFLINE/ERROR 覆盖。
    marker = {"resourceId": resource["id"], "stoppedAt": timestamp,
              "desiredState": "RUNNING", "reason": "AUTH_FAILED" if current_resource.get("authRejectedAt")
              else current_resource["healthStatus"]}
    changed = await repo.db.tasks.update_one(
        query, {"$set": {"desiredState": "STOPPED", "restartRequested": False,
                           "updatedAt": timestamp, "resourceHealthRecovery": marker}}, session=session,
    )
    if not changed.matched_count:
        return False
    await repo.db.operations.update_many(
        {"taskId": task["id"], "desiredState": {"$ne": "STOPPED"}, "status": "PENDING"},
        {"$set": {"status": "CANCELLED", "completedAt": timestamp}}, session=session,
    )
    operation_id = new_id()
    await repo.db.operations.insert_one({
        "id": operation_id, "taskId": task["id"], "desiredState": "STOPPED",
        "action": "system-resource-health-stop", "actor": "system", "status": "PENDING",
        "createdAt": timestamp,
    }, session=session)
    await repo.db.tasks.update_one(
        {"id": task["id"], "desiredState": "STOPPED", "controlClaimVersion": task.get("controlClaimVersion")},
        {"$set": {"controlOperationId": operation_id}}, session=session,
    )
    await repo.audit("system", "resource_health_stop:" + current_resource["healthStatus"], resource["id"], session=session)
    return True


async def stop_unresponsive_task(repo, task, *, liveness_check: Callable[[], bool] | None = None) -> bool:
    """供 Worker 在本地单调时钟证明十秒无可用日志后停止当前非成功 ISAPI 运行。"""
    async def commit(session):
        resource = await repo.db.resources.find_one(
            {"id": task.get("resourceId"), "deletedAt": None,
             "healthStatus": {"$in": sorted(BAD_HEALTH_STATUSES)}}, session=session,
        )
        if resource is None:
            return False
        return await stop_for_resource_health(
            repo, task, resource, session, require_owner=True, accepted_statuses=BAD_HEALTH_STATUSES,
            liveness_check=liveness_check,
        )

    return await audited_mutations.mutation_transaction(repo, commit)
