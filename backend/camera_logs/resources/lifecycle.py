"""协调软删除资源的任务停止、暂停运行回收与可重试完成状态。"""

import logging

from camera_logs.common.database import now

logger = logging.getLogger(__name__)


def task_resource_query(identifier: str) -> dict:
    """匹配资源作为任务主设备或串口服务器目标的两种关联方式。"""
    return {"$or": [{"resourceId": identifier}, {"serialServerResourceId": identifier}]}


async def deletion_cleanup_complete(repo, identifier: str) -> bool:
    """确认关联任务均已脱离节点、停止到终态且不再持有运行锁。"""
    query = task_resource_query(identifier)
    async for task in repo.db.tasks.find(query):
        if (task.get("nodeId") is not None or task.get("desiredState") != "STOPPED"
                or task.get("status") not in {"STOPPED", "ERROR", "BLOCKED"}):
            return False
        if await repo.db.endpoint_locks.count_documents({"taskId": task["id"]}):
            return False
    return True


async def reconcile_resource_deletion(repo, identifier: str) -> dict | None:
    """停止已删除资源的关联任务；失败保留 PENDING，下一次请求或调度周期可重试。"""
    resource = await repo.db.resources.find_one({"id": identifier, "deletedAt": {"$ne": None}})
    if resource is None:
        return None
    if resource.get("deletionState") == "DONE":
        return resource
    query = task_resource_query(identifier)
    async for task in repo.db.tasks.find(query):
        # 删除资源优先写停止意图；一旦写入，调度器的 RUNNING 领取条件不再匹配该任务。
        await repo.db.tasks.update_one({"id": task["id"], **query}, {"$set": {
            "desiredState": "STOPPED", "restartRequested": False, "resourceDeleted": True, "updatedAt": now(),
        }})
        await repo.db.operations.update_many(
            {"taskId": task["id"], "desiredState": {"$ne": "STOPPED"}, "status": "PENDING"},
            {"$set": {"status": "CANCELLED", "completedAt": now()}},
        )
        # 重新读取当前停止态快照。未归属节点的终态会话可按同一运行回收锁；
        # 保留 PAUSED 是为了处理调度器尚未将其推进到 STOPPED 的本轮过渡态。
        stopped = await repo.db.tasks.find_one({"id": task["id"], **query, "nodeId": None,
                                                "desiredState": "STOPPED",
                                                "status": {"$in": ["PAUSED", "STOPPED", "ERROR", "BLOCKED"]},
                                                "runId": {"$exists": True, "$nin": [None, ""]}})
        if stopped:
            await repo.db.endpoint_locks.delete_one({"taskId": stopped["id"], "runId": stopped["runId"]})
            await repo.db.runs.update_one(
                {"id": stopped["runId"], "endedAt": {"$exists": False}}, {"$set": {"endedAt": now()}}
            )
    if await deletion_cleanup_complete(repo, identifier):
        await repo.db.resources.update_one(
            {"id": identifier, "deletedAt": {"$ne": None}, "deletionState": "PENDING"},
            {"$set": {"deletionState": "DONE", "deletionCompletedAt": now(), "updatedAt": now()}},
        )
    return await repo.db.resources.find_one({"id": identifier})


async def reconcile_deleted_resources(repo, limit: int = 100) -> None:
    """由调度器重试此前 API 请求未能完成的资源停止扫尾，单资源失败不阻断其他资源。"""
    async for resource in repo.db.resources.find({"deletedAt": {"$ne": None}, "deletionState": "PENDING"}).limit(limit):
        try:
            await reconcile_resource_deletion(repo, resource["id"])
        except Exception:
            logger.exception("软删除资源停止扫尾失败 resource=%s", resource["id"])
