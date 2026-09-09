"""服务账号令牌的凭据变更与审计原子提交，提交不明时只读确认。"""

import hashlib
import secrets
from datetime import timedelta

from fastapi import HTTPException
from pymongo.errors import PyMongoError

from camera_logs.common import audited_mutations
from camera_logs.common.database import now, public
from camera_logs.common.models import new_id


async def _commit(repo, callback, confirmation):
    """数据库异常后只查询固定目标；绝不通过重跑写入猜测提交结果。"""
    try:
        return await audited_mutations.mutation_transaction(repo, callback)
    except PyMongoError as error:
        try:
            database = audited_mutations._majority_primary_database(repo)
            confirmed = await database.tokens.find_one(confirmation)
        except PyMongoError:
            confirmed = None
        if confirmed is not None:
            return confirmed
        raise HTTPException(503, "服务令牌提交结果未知，请查询令牌列表确认") from error


async def create_service_token(repo, actor_id, body):
    """事务重试复用固定凭据；数据库仅保存散列，明文仅在本次成功响应返回。"""
    token = secrets.token_urlsafe(32)
    timestamp = now()
    document = {
        "id": new_id(), "name": body.name, "scopes": body.scopes, "taskIds": body.taskIds,
        "tokenHash": hashlib.sha256(token.encode()).hexdigest(), "revoked": False,
        "expiresAt": timestamp + timedelta(days=body.expiresInDays), "createdAt": timestamp,
    }

    async def commit(session):
        await repo.db.tokens.insert_one(document, session=session)
        await repo.audit(actor_id, "create_token", document["id"], session=session)
        return document

    result = await _commit(repo, commit, {"id": document["id"], "tokenHash": document["tokenHash"]})
    return public(result) | {"token": token}


async def revoke_service_token(repo, actor_id, identifier):
    """只有首次撤销产生状态审计；并发或重试仍返回相同已撤销结果。"""
    async def commit(session):
        token = await repo.db.tokens.find_one({"id": identifier}, session=session)
        if token is None:
            raise HTTPException(404, "服务令牌不存在")
        if token.get("revoked"):
            return token
        await repo.db.tokens.update_one({"id": identifier}, {"$set": {"revoked": True}}, session=session)
        await repo.audit(actor_id, "revoke_token", identifier, session=session)
        return token | {"revoked": True}

    await _commit(repo, commit, {"id": identifier, "revoked": True})
