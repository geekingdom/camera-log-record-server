"""常用命令批量写入：事务内 CAS、审计和幂等结果必须同时完成。"""

from datetime import timedelta

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError, PyMongoError

from camera_logs.commands.models import CommonCommandBatchRequest
from camera_logs.common import audited_mutations
from camera_logs.common.database import now


def _result(body: CommonCommandBatchRequest) -> dict:
    """结果不回显命令正文，只说明动作与实际受影响行数。"""
    return {"action": body.action, "affected": len(body.items)}


async def _confirmed(database, actor_id: str, key: str, digest: str, *, session=None):
    """只有同摘要、已完成且带批量结果的映射才能被安全重放。"""
    existing = await database.idempotency.find_one({"actor": actor_id, "key": key}, session=session)
    if existing is None:
        return None
    if existing.get("digest") != digest:
        raise HTTPException(409, "幂等键已用于不同请求")
    if existing.get("state") != "SUCCEEDED" or not isinstance(existing.get("result"), dict):
        raise HTTPException(409, "原请求尚未完成或结果未知，未重复执行")
    return existing["result"]


async def apply_common_command_batch(repo, actor_id: str, key: str | None, body: CommonCommandBatchRequest) -> dict:
    """以一笔事务应用行版本快照，任一缺失或冲突都会使整个批次回滚。"""
    if not key or len(key) > 128:
        raise HTTPException(422, "必须提供不超过128字符的 Idempotency-Key")
    payload = body.model_dump()
    digest = audited_mutations.request_digest("batch_common_commands", payload)
    database = audited_mutations._majority_primary_database(repo)
    previous = await _confirmed(database, actor_id, key, digest)
    if previous is not None:
        return previous
    identifiers = [item.id for item in body.items]
    versions = {item.id: item.version for item in body.items}
    response = _result(body)

    async def commit(session):
        """先校验完整快照，随后批量 CAS、审计和重放映射一起提交。"""
        previous = await _confirmed(repo.db, actor_id, key, digest, session=session)
        if previous is not None:
            return previous
        documents = {item["id"]: item async for item in repo.db.common_commands.find({"id": {"$in": identifiers}}, session=session)}
        missing = [identifier for identifier in identifiers if identifier not in documents]
        if missing:
            raise HTTPException(404, "常用命令不存在")
        if any(documents[identifier].get("version") != versions[identifier] for identifier in identifiers):
            raise HTTPException(409, "常用命令版本已变化，请刷新")
        stamp = now()
        if body.action == "DELETE":
            for identifier in identifiers:
                changed = await repo.db.common_commands.delete_one(
                    {"id": identifier, "version": versions[identifier]}, session=session,
                )
                if changed.deleted_count != 1:
                    raise HTTPException(409, "常用命令版本已变化，请刷新")
        else:
            enabled = body.action == "ENABLE"
            for identifier in identifiers:
                changed = await repo.db.common_commands.update_one(
                    {"id": identifier, "version": versions[identifier]},
                    {"$set": {"enabled": enabled, "updatedAt": stamp}, "$inc": {"version": 1}}, session=session,
                )
                if changed.matched_count != 1:
                    raise HTTPException(409, "常用命令版本已变化，请刷新")
        action = f"batch_{body.action.lower()}_common_commands"
        for identifier in identifiers:
            await repo.audit(actor_id, action, identifier, session=session)
        await repo.db.idempotency.insert_one({
            "actor": actor_id, "key": key, "digest": digest, "resourceId": "common-command-batch",
            "result": response, "state": "SUCCEEDED", "updatedAt": stamp, "expiresAt": stamp + timedelta(days=7),
        }, session=session)
        return response

    try:
        return await audited_mutations.mutation_transaction(repo, commit)
    except DuplicateKeyError as error:
        previous = await _confirmed(database, actor_id, key, digest)
        if previous is not None:
            return previous
        raise HTTPException(409, "幂等键冲突，请使用原请求重试") from error
    except HTTPException:
        raise
    except PyMongoError as error:
        try:
            previous = await _confirmed(database, actor_id, key, digest)
        except PyMongoError:
            previous = None
        if previous is not None:
            return previous
        raise HTTPException(503, "批量操作结果未知，请使用相同幂等键重试") from error
