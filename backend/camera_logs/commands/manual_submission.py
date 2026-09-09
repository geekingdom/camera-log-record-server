"""手动命令、幂等映射和操作审计原子提交，确认不明时仅查询原命令。"""

import asyncio
from datetime import timedelta

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError, PyMongoError

from camera_logs.commands.manual_admission import admit_manual
from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.models import new_id


async def _confirmed(database, actor, key, digest, *, session=None):
    """历史PENDING只有已存在原命令时才返回，绝不重新入队或补发。"""
    existing = await database.idempotency.find_one({"actor": actor, "key": key}, session=session)
    if existing and existing.get("state") == "PENDING" and existing.get("digest") == digest:
        previous = await database.commands.find_one({"id": existing.get("resourceId")}, session=session)
        if previous is not None:
            return previous
    return await audited_mutations._confirmed_existing(database, digest, "commands", existing, session=session)


async def submit_manual(repo, actor, task_id, key, body):
    """先重放同键已提交结果，再检查新命令可交互状态，设备发送不属于本事务。"""
    if not key or len(key) > 128:
        raise HTTPException(422, "必须提供不超过128字符的 Idempotency-Key")
    action = "command:" + task_id
    digest = audited_mutations.request_digest(action, body)
    database = audited_mutations._majority_primary_database(repo)
    previous = await _confirmed(database, actor, key, digest)
    if previous is not None:
        return previous
    task = await repo.get("tasks", task_id)
    if (task["status"] != "COLLECTING" or task["desiredState"] != "RUNNING"
            or not task.get("sessionId") or not task.get("runId")):
        raise HTTPException(409, "任务未处于可交互采集状态")
    identifier = new_id()

    async def commit(session):
        """命令领取与审计可见性同步；任一写失败都不能留下可发送的命令。"""
        previous = await _confirmed(repo.db, actor, key, digest, session=session)
        if previous is not None:
            return previous
        timestamp = now()
        await repo.db.idempotency.insert_one({
            "actor": actor, "key": key, "digest": digest, "resourceId": identifier,
            "state": "SUCCEEDED", "updatedAt": timestamp, "expiresAt": timestamp + timedelta(days=7),
        }, session=session)
        document = await admit_manual(repo, task, identifier, body, actor, session=session)
        await repo.audit(actor, action, identifier, session=session)
        return document

    try:
        return await audited_mutations.mutation_transaction(repo, commit)
    except (HTTPException, asyncio.CancelledError):
        raise
    except PyMongoError as error:
        try:
            previous = await _confirmed(database, actor, key, digest)
        except PyMongoError:
            previous = None
        if previous is not None:
            return previous
        if isinstance(error, DuplicateKeyError):
            raise HTTPException(409, "幂等键冲突，请使用原请求查询") from error
        raise HTTPException(503, "命令提交结果未知，请使用相同幂等键重试，未重复发送") from error
