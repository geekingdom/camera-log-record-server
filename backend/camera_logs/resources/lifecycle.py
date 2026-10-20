"""协调软删除资源的任务停止、暂停运行回收与可重试完成状态。"""

import logging

from camera_logs.common.database import now

logger = logging.getLogger(__name__)


def task_resource_query(identifier: str) -> dict:
    """匹配资源作为任务主设备或串口服务器目标的两种关联方式。"""
    return {"$or": [{"resourceId": identifier}, {"serialServerResourceId": identifier}]}


async def _task_deletion_complete(repo, task: dict) -> bool:
    """只在连接归属、停止意图和运行锁均已回收后确认任务删除完成。"""
    if (task.get("nodeId") is not None or task.get("desiredState") != "STOPPED"
            or task.get("status") not in {"STOPPED", "ERROR", "BLOCKED"}):
        return False
    return not await repo.db.endpoint_locks.count_documents({"taskId": task["id"]})


async def reconcile_task_deletion(repo, identifier: str, *, resource_deleted: bool = False) -> dict | None:
    """推进单个已软删任务的受控停止，绝不触碰日志目录、归档或下载制品。"""
    task = await repo.db.tasks.find_one({"id": identifier, "deletedAt": {"$ne": None}})
    if task is None:
        return None
    timestamp = now()
    changes = {"desiredState": "STOPPED", "restartRequested": False, "updatedAt": timestamp}
    if resource_deleted:
        changes["resourceDeleted"] = True
    await repo.db.tasks.update_one({"id": identifier, "deletedAt": {"$ne": None}}, {"$set": changes})
    await repo.db.operations.update_many(
        {"taskId": identifier, "desiredState": {"$ne": "STOPPED"}, "status": "PENDING"},
        {"$set": {"status": "CANCELLED", "completedAt": timestamp}},
    )
    # 已暂停任务没有 owner 时没有 Worker 会再把它改为停止；软删除必须自行结束该运行。
    await repo.db.tasks.update_one(
        {"id": identifier, "deletedAt": {"$ne": None}, "nodeId": None,
         "desiredState": "STOPPED", "status": "PAUSED"},
        {"$set": {"status": "STOPPED", "updatedAt": timestamp}},
    )
    # 未被节点持有的历史运行可以立刻精确释放；活跃连接只能由 owner Worker 关闭。
    stopped = await repo.db.tasks.find_one({
        "id": identifier, "deletedAt": {"$ne": None}, "nodeId": None, "desiredState": "STOPPED",
        "status": {"$in": ["PAUSED", "STOPPED", "ERROR", "BLOCKED"]},
        "runId": {"$exists": True, "$nin": [None, ""]},
    })
    if stopped:
        await repo.db.endpoint_locks.delete_one({"taskId": identifier, "runId": stopped["runId"]})
        await repo.db.runs.update_one(
            {"id": stopped["runId"], "endedAt": {"$exists": False}}, {"$set": {"endedAt": timestamp}}
        )
    current = await repo.db.tasks.find_one({"id": identifier})
    if current and await _task_deletion_complete(repo, current):
        await repo.db.tasks.update_one(
            {"id": identifier, "deletedAt": {"$ne": None}, "deletionState": "PENDING"},
            {"$set": {"deletionState": "DONE", "deletionCompletedAt": timestamp, "updatedAt": timestamp}},
        )
    return await repo.db.tasks.find_one({"id": identifier})


async def reconcile_deleted_tasks(repo, limit: int = 500) -> None:
    """由调度周期重试已删除任务的运行收尾，单项故障不能阻断其它任务。"""
    async for task in repo.db.tasks.find({"deletedAt": {"$ne": None}, "deletionState": "PENDING"}).limit(limit):
        try:
            await reconcile_task_deletion(repo, task["id"])
        except Exception:
            logger.exception("软删除任务停止扫尾失败 task=%s", task["id"])


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
    query = task_resource_query(identifier)
    # 兼容早期资源软删除：这些记录可能已写成 DONE，却没有给关联任务写 deletedAt。
    # 先补齐可见任务的删除标记，再根据实际连接回收结果决定是否重新进入 PENDING。
    visible = [task async for task in repo.db.tasks.find(
        {"$and": [query, {"deletedAt": None}]}, {"id": 1},
    )]
    if visible:
        timestamp = resource.get("deletedAt") or now()
        identifiers = [task["id"] for task in visible]
        await repo.db.tasks.update_many(
            {"id": {"$in": identifiers}, "deletedAt": None},
            {"$set": {"deletedAt": timestamp, "deletionState": "PENDING", "desiredState": "STOPPED",
                      "restartRequested": False, "resourceDeleted": True, "updatedAt": timestamp}},
        )
        await repo.db.operations.update_many(
            {"taskId": {"$in": identifiers}, "desiredState": {"$ne": "STOPPED"}, "status": "PENDING"},
            {"$set": {"status": "CANCELLED", "completedAt": timestamp}},
        )
        await repo.db.resources.update_one(
            {"id": identifier, "deletedAt": {"$ne": None}},
            {"$set": {"deletionState": "PENDING", "updatedAt": timestamp}},
        )
        resource["deletionState"] = "PENDING"
    if resource.get("deletionState") == "DONE":
        return resource
    async for task in repo.db.tasks.find(query):
        # 当前任务已由删除事务或旧记录补偿路径标记；这里只做可重试的连接/锁收尾。
        await reconcile_task_deletion(repo, task["id"], resource_deleted=True)
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
