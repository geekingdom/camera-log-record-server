"""阻塞采集运行的关闭收据与单任务外部隔离确认。"""

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.common.observability import redact
from camera_logs.common.ownership import owner_filter
from camera_logs.common.security import authorize
from camera_logs.tasks.control import _guard_resources


async def record_closed_receipt(repo, task, instance_id: str, session_id: str | None) -> None:
    """在传输和日志收尾完成后固化精确 owner 收据，供数据库收尾重试使用。"""
    if not session_id:
        raise RuntimeError("运行关闭后缺少实际会话标识，不能确认收据")
    receipt = {
        "taskId": task["id"], "runId": task["runId"], "generation": task.get("generation"),
        "nodeId": task.get("nodeId"), "sessionId": session_id,
        "instanceId": instance_id, "closedAt": now(),
    }
    await repo.db.tasks.update_one(owner_filter(task), {"$set": {"closedReceipt": receipt}})


def matching_closed_receipt(task) -> bool:
    """只信任与当前任务归属完整相同的收据，旧 run 或后继 owner 一律不可复用。"""
    receipt = task.get("closedReceipt") or {}
    keys = ("runId", "generation", "nodeId", "sessionId")
    return all(task.get(key) is not None and receipt.get(key) == task.get(key) for key in keys) \
        and receipt.get("taskId") == task.get("id") and bool(receipt.get("instanceId")) \
        and receipt.get("closedAt") is not None


async def accepted_restart(repo, task, session):
    """仅重放当前控制指针的已接受恢复，不能复用用户停止前的旧请求。"""
    if task.get("desiredState") != "RUNNING" or task.get("status") == "BLOCKED":
        return None
    statuses = ["PENDING"]
    if task.get("status") == "COLLECTING":
        statuses.append("SUCCEEDED")
    return await repo.db.operations.find_one({
        "id": task.get("controlOperationId"), "taskId": task["id"],
        "action": "restart-blocked", "status": {"$in": statuses},
    }, session=session)


async def finish_blocked_run(repo, task, session, *, restart, operation_id=None, evidence=None):
    """在调用方事务内收尾已证实关闭的运行；不删除后继锁，不提前完成重启。"""
    task_id, run_id, timestamp = task["id"], task.get("runId"), now()
    foreign = await repo.db.endpoint_locks.find_one(
        {"taskId": task_id, "runId": {"$ne": run_id}}, session=session,
    )
    if foreign:
        raise HTTPException(409, "任务存在后继运行锁，不能释放旧运行")
    await repo.db.endpoint_locks.delete_one({"taskId": task_id, "runId": run_id}, session=session)
    await repo.db.runs.update_one({"id": run_id, "taskId": task_id}, {"$set": {"endedAt": timestamp}}, session=session)
    for old, new in (("SENDING", "UNKNOWN"), ("QUEUED", "CANCELLED")):
        await repo.db.commands.update_many({"taskId": task_id, "runId": run_id, "status": old},
            {"$set": {"status": new, "completedAt": timestamp}}, session=session)
    update = {"nodeId": None, "status": "STOPPED", "desiredState": "RUNNING" if restart else "STOPPED",
              "restartRequested": False, "error": None, "updatedAt": timestamp}
    if operation_id:
        update["controlOperationId"] = operation_id
    if evidence is not None:
        update["isolationEvidence"] = str(redact(evidence))
    changed = await repo.db.tasks.update_one(
        owner_filter(task) | {"status": "BLOCKED", "controlOperationId": task.get("controlOperationId")},
        {"$set": update}, session=session,
    )
    if not changed.matched_count:
        raise HTTPException(409, "任务控制意图或旧运行归属已变化，请重新查询")
    if operation_id:
        await repo.db.operations.update_one({"id": operation_id, "taskId": task_id, "status": "PENDING"}, {
            "$unset": {"phase": ""}, "$set": {"updatedAt": timestamp},
        }, session=session)
    await repo.db.operations.update_many({"taskId": task_id, "desiredState": "STOPPED", "status": "PENDING"}, {
        "$set": {"status": "SUCCEEDED", "completedAt": timestamp}, "$unset": {"phase": ""},
    }, session=session)
    if not restart:
        await repo.db.operations.update_many({"taskId": task_id, "desiredState": "RUNNING", "status": "PENDING"}, {
            "$set": {"status": "CANCELLED", "completedAt": timestamp}, "$unset": {"phase": ""},
        }, session=session)


async def finalize_closed_task(repo, task):
    """Worker 无运行对象时消费精确关闭收据；重新读取控制意图避免覆盖并发停止。"""
    async def commit(session):
        current = await repo.db.tasks.find_one_and_update(
            owner_filter(task) | {"status": "BLOCKED"}, {"$inc": {"controlClaimVersion": 1}},
            return_document=ReturnDocument.AFTER, session=session,
        )
        if current is None or not matching_closed_receipt(current):
            return False
        restart = bool(current.get("restartRequested"))
        if not restart and current.get("desiredState") != "STOPPED":
            return False
        operation_id = current.get("controlOperationId")
        if restart:
            operation = await repo.db.operations.find_one({"id": operation_id, "taskId": current["id"],
                "action": "restart-blocked", "status": "PENDING"}, session=session)
            if operation is None:
                return False
            await _guard_resources(repo.db, current, session)
        await finish_blocked_run(repo, current, session, restart=restart, operation_id=operation_id)
        return True

    return await audited_mutations.mutation_transaction(repo, commit)


async def confirm_task_isolation(repo, task_id: str, user, evidence: str):
    """管理员确认单个旧运行已隔离，只释放该任务的锁和运行记录。"""
    authorize(user, "admin")
    identifier = new_id()

    async def commit(session):
        task = await repo.db.tasks.find_one_and_update(
            {"id": task_id}, {"$inc": {"controlClaimVersion": 1}},
            return_document=ReturnDocument.AFTER, session=session,
        )
        if task is None:
            raise HTTPException(404, "任务不存在")
        accepted = await accepted_restart(repo, task, session)
        if accepted:
            return accepted
        if task.get("status") != "BLOCKED":
            raise HTTPException(409, "任务不处于阻塞状态")
        await _guard_resources(repo.db, task, session)
        timestamp = now()
        existing = await repo.db.operations.find_one(
            {"taskId": task_id, "action": "restart-blocked", "status": "PENDING"}, session=session,
        )
        operation = existing or {"id": identifier, "taskId": task_id, "desiredState": "RUNNING",
                                 "action": "restart-blocked", "actor": user["id"], "status": "PENDING",
                                 "createdAt": timestamp}
        await finish_blocked_run(repo, task, session, restart=True, operation_id=operation["id"], evidence=evidence)
        operation.pop("phase", None)
        if existing is None:
            await repo.db.operations.insert_one(operation, session=session)
        await repo.audit(user["id"], "confirm_task_isolation", task_id, session=session)
        return operation

    try:
        return await audited_mutations.mutation_transaction(repo, commit)
    except PyMongoError as error:
        raise HTTPException(503, "单任务隔离确认提交结果未知，请查询任务与操作状态") from error
