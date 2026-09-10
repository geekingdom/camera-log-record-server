"""BLOCKED 任务的受控重新启动意图，保留旧 owner 直到关闭被确认。"""

from datetime import UTC, timedelta

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.common.security import authorize, authorize_owner
from camera_logs.node.recovery import accepted_restart, finish_blocked_run, matching_closed_receipt
from camera_logs.tasks.control import _guard_resources


async def request_blocked_restart(repo, task_id, user):
    """登记或复用恢复操作；旧 Worker 收尾后才允许调度器领取新运行。"""
    authorize(user, "tasks:control", task_id)
    identifier = new_id()

    async def commit(session):
        task = await repo.db.tasks.find_one_and_update(
            {"id": task_id}, {"$inc": {"controlClaimVersion": 1}},
            return_document=ReturnDocument.AFTER, session=session,
        )
        if task is None:
            raise HTTPException(404, "任务不存在")
        authorize_owner(user, task)
        accepted = await accepted_restart(repo, task, session)
        if accepted:
            return accepted
        if task.get("status") != "BLOCKED":
            raise HTTPException(409, "仅阻塞任务可以请求重新启动")
        await _guard_resources(repo.db, task, session)
        previous = await repo.db.operations.find_one(
            {"taskId": task_id, "action": "restart-blocked", "status": "PENDING"}, session=session,
        )
        if matching_closed_receipt(task):
            timestamp = now()
            operation = previous or {"id": identifier, "taskId": task_id, "desiredState": "RUNNING", "action": "restart-blocked",
                                     "actor": user["id"], "status": "PENDING", "createdAt": timestamp}
            await finish_blocked_run(repo, task, session, restart=True, operation_id=operation["id"])
            operation.pop("phase", None)
            if previous is None:
                await repo.db.operations.insert_one(operation, session=session)
            await repo.audit(user["id"], "restart_blocked_receipt", task_id, session=session)
            return operation
        if previous:
            return previous
        node = await repo.db.nodes.find_one({"id": task.get("nodeId")}, session=session)
        heartbeat = node.get("heartbeat") if node else None
        if not node or not heartbeat or heartbeat.replace(tzinfo=UTC) < now() - timedelta(seconds=30):
            raise HTTPException(409, {"code": "ISOLATION_REQUIRED", "message": "旧节点不可达，必须确认单任务隔离"})
        timestamp = now()
        operation = {"id": identifier, "taskId": task_id, "desiredState": "RUNNING", "action": "restart-blocked",
                     "actor": user["id"], "status": "PENDING", "createdAt": timestamp}
        await repo.db.operations.update_many(
            {"taskId": task_id, "desiredState": "RUNNING", "status": "PENDING"},
            {"$set": {"status": "CANCELLED", "completedAt": timestamp}}, session=session,
        )
        await repo.db.operations.insert_one(operation, session=session)
        await repo.db.tasks.update_one(
            {"id": task_id, "status": "BLOCKED", "nodeId": task.get("nodeId"), "runId": task.get("runId"),
             "generation": task.get("generation")},
            {"$set": {"desiredState": "STOPPED", "restartRequested": True,
                      "controlOperationId": identifier, "updatedAt": timestamp}}, session=session,
        )
        await repo.audit(user["id"], "restart_blocked", task_id, session=session)
        return operation

    try:
        return await audited_mutations.mutation_transaction(repo, commit)
    except PyMongoError as error:
        raise HTTPException(503, "阻塞任务恢复提交结果未知，请查询任务与操作状态") from error
