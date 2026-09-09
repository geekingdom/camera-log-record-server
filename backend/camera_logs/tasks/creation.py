"""原子创建任务及自动启动意图，避免创建映射与控制操作跨事务分离。"""

import asyncio
from datetime import timedelta

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError, PyMongoError

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.models import new_id


async def _claim_resources(db, document, session):
    """写入关联资源声明版本，与软删除事务产生写冲突，阻止迟到任务启动。"""
    identifiers = sorted({item for item in (
        document["resourceId"], document.get("serialServerResourceId")
    ) if item})
    for identifier in identifiers:
        resource = await db.resources.find_one_and_update(
            {"id": identifier, "deletedAt": None}, {"$inc": {"controlClaimVersion": 1}},
            session=session,
        )
        if resource is None:
            if await db.resources.find_one({"id": identifier}, session=session) is None:
                raise HTTPException(404, "设备资源不存在")
            raise HTTPException(409, "设备资源已删除，任务不会启动")


def _response(document):
    """为自动启动的创建响应附加可轮询操作，不把响应字段写回任务。"""
    result = document.copy()
    if result.get("creationOperationId"):
        result["operationId"] = result["creationOperationId"]
    return result


async def _confirmed(database, actor, key, digest, *, session=None):
    """只接受同键成功映射和仍存在任务，未确认结果不得重新准备或创建。"""
    existing = await database.idempotency.find_one({"actor": actor, "key": key}, session=session)
    # 旧 Repository.idem 在任务已落库后仍保留 PENDING 映射。仅当摘要相同且
    # 原任务仍存在时兼容返回，避免部署后把历史同键重试变为 409；新路径只写 SUCCEEDED。
    if existing and existing.get("state") == "PENDING" and existing.get("digest") == digest:
        legacy = await database.tasks.find_one({"id": existing.get("resourceId")}, session=session)
        if legacy is not None and legacy.get("deletedAt") is None:
            return _response(legacy)
    task = await audited_mutations._confirmed_existing(
        database, digest, "tasks", existing, session=session,
    )
    return _response(task) if task is not None else None


async def create_task(repo, user, key, payload, prepare):
    """创建任务并原子保存自动启动操作、审计和幂等成功映射。

    ``prepare`` 只在事务外执行一次，用固定任务 ID 完成资源绑定、命令复制和
    密码加密；事务回调只写数据库，因此驱动重试不会重复这些准备工作。
    """
    if not key or len(key) > 128:
        raise HTTPException(422, "必须提供不超过128字符的 Idempotency-Key")
    actor, action = user["id"], "create_task"
    digest = audited_mutations.request_digest(action, payload)
    database = audited_mutations._majority_primary_database(repo)
    existing = await _confirmed(database, actor, key, digest)
    if existing is not None:
        return existing

    identifier, operation_id = new_id(), new_id()
    document, auto_start = await prepare(identifier)
    if not isinstance(document, dict) or document.get("id") != identifier:
        raise ValueError("任务准备必须返回固定 id 的文档")

    async def commit(session):
        """写入完整创建结果；任何一个写入或审计失败都由事务整体回滚。"""
        current = await _confirmed(repo.db, actor, key, digest, session=session)
        if current is not None:
            return current
        await _claim_resources(repo.db, document, session)
        timestamp = now()
        task = document.copy()
        task.update(desiredState="RUNNING" if auto_start else "STOPPED", status="STOPPED", nodeId=None,
                    createdAt=timestamp, updatedAt=timestamp, generation=0,
                    createdBy=user["id"], createdByName=user.get("displayName") or user.get("username") or user["id"])
        if auto_start:
            task["controlOperationId"] = operation_id
            # 后续控制会更新controlOperationId；创建重放仍应返回最初的自动启动操作。
            task["creationOperationId"] = operation_id
        await repo.db.idempotency.insert_one(
            {"actor": actor, "key": key, "digest": digest, "resourceId": identifier,
             "state": "SUCCEEDED", "updatedAt": timestamp,
             "expiresAt": timestamp + timedelta(days=7)}, session=session,
        )
        await repo.db.tasks.insert_one(task, session=session)
        if auto_start:
            await repo.db.operations.insert_one(
                {"id": operation_id, "taskId": identifier, "desiredState": "RUNNING",
                 "action": "start", "actor": actor, "status": "PENDING", "createdAt": timestamp},
                session=session,
            )
        await repo.audit(actor, action, identifier, session=session)
        if auto_start:
            await repo.audit(actor, "control:RUNNING", identifier, session=session)
        return _response(task)

    try:
        return await audited_mutations.mutation_transaction(repo, commit)
    except (HTTPException, asyncio.CancelledError):
        raise
    except (DuplicateKeyError, PyMongoError) as error:
        try:
            confirmed = await _confirmed(database, actor, key, digest)
        except PyMongoError:
            confirmed = None
        if confirmed is not None:
            return confirmed
        if isinstance(error, DuplicateKeyError):
            raise HTTPException(409, "幂等键已用于不同请求") from error
        raise HTTPException(503, "任务创建提交结果未知，请使用相同幂等键重试") from error
