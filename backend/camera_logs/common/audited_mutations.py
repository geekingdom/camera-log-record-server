"""以 MongoDB 事务提交业务变更、审计事件和可确认的幂等创建。"""

import asyncio
import hashlib
import json
import logging
from datetime import timedelta

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

from camera_logs.common.database import now
from camera_logs.common.models import new_id

logger = logging.getLogger(__name__)


def request_digest(action, payload):
    """计算动作和请求体的稳定摘要，绝不把请求正文写入日志或异常文本。"""
    encoded = json.dumps([action, payload], sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


async def mutation_transaction(repo, callback):
    """以快照读取、多数确认和 journal 提交事务，驱动可重试 callback。"""
    async with repo.db.client.start_session() as session:
        return await session.with_transaction(
            callback,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority", j=True),
        )


async def audited_mutation(repo, actor, action, target, callback):
    """将数据库业务回调和审计写入同一事务，避免成功变更缺少审计。"""
    async def commit(session):
        result = await callback(session)
        await repo.audit(actor, action, target, session=session)
        return result

    return await _commit(repo, commit)


async def audited_create(repo, actor, key, action, payload, collection, prepare):
    """创建资源和成功映射并原子审计，事务重试不重复运行 prepare。

    prepare 可能包含事务外的可控准备工作，因此固定资源标识并在进入事务前仅调用
    一次。事务内只允许数据库写入；发生提交确认不明时，只有读到同键成功映射及
    对象后才能返回成功，否则调用方必须以相同幂等键重试。
    """
    if not key or len(key) > 128:
        raise HTTPException(422, "必须提供不超过128字符的 Idempotency-Key")
    digest = request_digest(action, payload)
    database = _majority_primary_database(repo)
    existing = await database.idempotency.find_one({"actor": actor, "key": key})
    cached = await _confirmed_existing(database, digest, collection, existing)
    if cached is not None:
        return cached

    identifier = new_id()
    document = await prepare(identifier)
    if not isinstance(document, dict) or document.get("id") != identifier:
        raise ValueError("prepare 必须返回带固定 id 的数据库文档")

    async def commit(session):
        current = await repo.db.idempotency.find_one(
            {"actor": actor, "key": key}, session=session
        )
        result = await _confirmed_existing(
            repo.db, digest, collection, current, session=session
        )
        if result is not None:
            return result
        await repo.db.idempotency.insert_one(
            {
                "actor": actor,
                "key": key,
                "digest": digest,
                "resourceId": identifier,
                "state": "SUCCEEDED",
                "updatedAt": now(),
                "expiresAt": now() + timedelta(days=7),
            },
            session=session,
        )
        await repo.db[collection].insert_one(document, session=session)
        await repo.audit(actor, action, identifier, session=session)
        return document

    try:
        return await mutation_transaction(repo, commit)
    except DuplicateKeyError as error:
        confirmed = await _recover_confirmed(repo, actor, key, digest, collection)
        if confirmed is not None:
            return confirmed
        raise HTTPException(409, "创建对象已存在或幂等键冲突") from error
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except PyMongoError as error:
        confirmed = await _recover_confirmed(repo, actor, key, digest, collection)
        if confirmed is not None:
            return confirmed
        logger.warning("审计创建提交结果未知 actor=%s action=%s", actor, action)
        raise HTTPException(503, "创建提交结果未知，请使用相同幂等键重试") from error


async def _commit(repo, callback):
    """统一保留业务异常，基础设施异常只报告事务结果未知。"""
    try:
        return await mutation_transaction(repo, callback)
    except (HTTPException, DuplicateKeyError, asyncio.CancelledError):
        raise
    except PyMongoError as error:
        logger.warning("审计事务结果未知")
        raise HTTPException(503, "事务提交结果未知，请查询最新状态后再操作") from error


async def _read_confirmed(repo, actor, key, digest, collection):
    """在事务错误后以当前多数可见状态确认是否已提交，未确认则返回 None。"""
    database = _majority_primary_database(repo)
    existing = await database.idempotency.find_one({"actor": actor, "key": key})
    return await _confirmed_existing(database, digest, collection, existing)


def _majority_primary_database(repo):
    """幂等结果只从多数确认的主库读取，避免把未确认提交误作成功。"""
    return repo.db.with_options(
        read_concern=ReadConcern("majority"), read_preference=ReadPreference.PRIMARY
    )


async def _recover_confirmed(repo, actor, key, digest, collection):
    """提交异常后的只读恢复不得遮蔽幂等冲突，也不把读故障误判为回滚。"""
    try:
        return await _read_confirmed(repo, actor, key, digest, collection)
    except PyMongoError:
        return None


async def _confirmed_existing(database, digest, collection, existing, *, session=None):
    """验证同键映射和对象；冲突或未知状态不能触发第二次 prepare。"""
    if existing is None:
        return None
    if existing.get("digest") != digest:
        raise HTTPException(409, "幂等键已用于不同请求")
    if existing.get("state") != "SUCCEEDED":
        raise HTTPException(409, "原请求尚未完成或结果未知，未重复执行")
    result = await database[collection].find_one(
        {"id": existing.get("resourceId")}, session=session
    )
    if result is None or result.get("deletedAt") is not None:
        raise HTTPException(410, "原请求创建的对象已删除")
    return result
