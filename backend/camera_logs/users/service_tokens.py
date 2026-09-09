"""服务账号令牌的凭据变更与审计原子提交，提交不明时只读确认。"""

import hashlib
import secrets
from datetime import timedelta

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from camera_logs.common import audited_mutations
from camera_logs.common.database import now, public
from camera_logs.common.errors import StableCodeHTTPException
from camera_logs.common.models import new_id


def _effective_status(document, user):
    """按令牌自身与绑定用户的当前状态计算可执行性，不缓存认证结论。"""
    if document.get("revoked"):
        return "REVOKED"
    expires_at = document.get("expiresAt")
    if expires_at is not None and expires_at.replace(tzinfo=now().tzinfo) <= now():
        return "EXPIRED"
    if user is None:
        return "USER_MISSING"
    if user.get("deletedAt") is not None:
        return "USER_DELETED"
    if not user.get("enabled"):
        return "USER_DISABLED"
    return "ACTIVE"


def public_token(document, user=None):
    """返回令牌、绑定用户和实时状态，绝不暴露令牌散列或账号敏感字段。"""
    result = public(document)
    result["effectiveStatus"] = _effective_status(document, user)
    if user is not None:
        result["user"] = {key: user.get(key) for key in ("id", "username", "displayName", "enabled", "deletedAt")}
    return result


async def readable_service_token(repo, identity, identifier):
    """读取可由当前主体查看的令牌；非管理员不得借标识探测他人凭据。"""
    document = await repo.db.tokens.find_one({"id": identifier})
    if document is None:
        raise HTTPException(404, "服务令牌不存在")
    if not identity.get("isAdmin") and "*" not in identity["scopes"] and document.get("userId") != identity["id"]:
        raise HTTPException(403, "无权查看此服务令牌")
    return document


async def reveal_service_token(repo, identity, identifier):
    """审计后复核版本和归属再返回明文，避免并发改绑泄露旧凭据。"""
    document = await readable_service_token(repo, identity, identifier)
    encrypted = document.get("tokenEncrypted")
    if not encrypted:
        raise StableCodeHTTPException(409, "SERVICE_TOKEN_LEGACY_SECRET", "旧服务令牌未保存可恢复的明文，请使用轮换生成新令牌")
    try:
        token = repo.decrypt(encrypted)
    except (InvalidToken, UnicodeDecodeError) as error:
        raise HTTPException(503, "服务令牌密文无法解密，请联系管理员轮换") from error
    try:
        await repo.audit(identity["id"], "reveal_token", identifier)
    except PyMongoError as error:
        raise HTTPException(503, "服务令牌查看审计失败，请稍后重试") from error
    current = await repo.db.tokens.find_one({
        "id": identifier,
        "version": document["version"],
        "userId": document["userId"],
    })
    if current is None:
        raise StableCodeHTTPException(409, "SERVICE_TOKEN_CHANGED", "服务令牌已变化，请刷新后重试")
    return {"token": token}


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
    """将固定凭据绑定到有效既有用户，权限不写入令牌而在认证时实时读取。"""
    user = await repo.db.users.find_one({"id": body.userId, "enabled": True, "deletedAt": None})
    if user is None:
        raise HTTPException(422, "绑定用户不存在或已停用")
    token = secrets.token_urlsafe(32)
    timestamp = now()
    document = {
        "id": new_id(), "name": body.name, "userId": user["id"], "version": 1,
        "tokenHash": hashlib.sha256(token.encode()).hexdigest(), "tokenEncrypted": repo.encrypt(token), "revoked": False,
        "expiresAt": timestamp + timedelta(days=body.expiresInDays) if body.expiresInDays is not None else None,
        "createdAt": timestamp,
    }

    async def commit(session):
        # 事务开始前的预检不能替代提交时的有效性确认，避免并发停用后仍生成凭据。
        current_user = await repo.db.users.find_one(
            {"id": document["userId"], "enabled": True, "deletedAt": None}, session=session
        )
        if current_user is None:
            raise HTTPException(422, "绑定用户不存在或已停用")
        await repo.db.tokens.insert_one(document, session=session)
        await repo.audit(actor_id, "create_token", document["id"], session=session)
        return document

    result = await _commit(repo, commit, {"id": document["id"], "tokenHash": document["tokenHash"]})
    return public_token(result, user) | {"token": token}


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


async def update_service_token(repo, actor_id, identifier, body):
    """以版本锁更新令牌元数据；过期令牌不能借有效期编辑恢复认证。"""
    values = body.model_dump(exclude={"version"}, exclude_unset=True)
    if not values:
        raise HTTPException(422, "至少提供令牌名称或绑定用户")
    has_expiry_change = "expiresInDays" in values
    expires_in_days = values.pop("expiresInDays", None)
    event_id = new_id()

    async def commit(session):
        current = await repo.db.tokens.find_one({"id": identifier}, session=session)
        if current is None:
            raise HTTPException(404, "服务令牌不存在")
        if current["version"] != body.version:
            raise HTTPException(409, "服务令牌版本已变化，请刷新")
        expires_at = current.get("expiresAt")
        if has_expiry_change and expires_at is not None and expires_at.replace(tzinfo=now().tzinfo) <= now():
            raise HTTPException(409, "服务令牌已过期，不能修改有效期")
        if "userId" in values:
            user = await repo.db.users.find_one(
                {"id": values["userId"], "enabled": True, "deletedAt": None}, session=session
            )
            if user is None:
                raise HTTPException(422, "绑定用户不存在或已停用")
        updates = values | ({"expiresAt": now() + timedelta(days=expires_in_days)}
                             if has_expiry_change and expires_in_days is not None else
                             {"expiresAt": None} if has_expiry_change else {})
        changed = await repo.db.tokens.find_one_and_update(
            {"id": identifier, "version": body.version},
            {"$set": updates, "$inc": {"version": 1}},
            return_document=ReturnDocument.AFTER, session=session,
        )
        if changed is None:
            raise HTTPException(409, "服务令牌版本已变化，请刷新")
        await repo.audit(actor_id, "update_token", identifier, session=session, event_id=event_id)
        return changed

    try:
        changed = await audited_mutations.mutation_transaction(repo, commit)
    except PyMongoError as error:
        # 版本只能说明有某次写入完成。固定审计 ID 与本次令牌写入同事务，才能确认
        # 提交异常后返回的是本请求的结果，而不是并发管理员的同版本更新。
        try:
            database = audited_mutations._majority_primary_database(repo)
            confirmed = await database.audit.find_one({"_id": event_id, "action": "update_token", "targetId": identifier})
            changed = await database.tokens.find_one({"id": identifier}) if confirmed else None
        except PyMongoError:
            changed = None
        if changed is None:
            raise HTTPException(503, "服务令牌提交结果未知，请查询令牌列表确认") from error
    user = await repo.db.users.find_one({"id": changed["userId"]})
    return public_token(changed, user)


async def rotate_service_token(repo, actor_id, identifier, version):
    """原子替换令牌密文与散列，提交不明时只确认本次固定凭据和审计。"""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_encrypted = repo.encrypt(token)
    event_id = new_id()

    async def commit(session):
        current = await repo.db.tokens.find_one({"id": identifier}, session=session)
        if current is None:
            raise HTTPException(404, "服务令牌不存在")
        if current["version"] != version:
            raise HTTPException(409, "服务令牌版本已变化，请刷新")
        changed = await repo.db.tokens.find_one_and_update(
            {"id": identifier, "version": version},
            {"$set": {"tokenHash": token_hash, "tokenEncrypted": token_encrypted, "updatedAt": now()}, "$inc": {"version": 1}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if changed is None:
            raise HTTPException(409, "服务令牌版本已变化，请刷新")
        await repo.audit(actor_id, "rotate_token", identifier, session=session, event_id=event_id)
        return changed

    try:
        changed = await audited_mutations.mutation_transaction(repo, commit)
    except PyMongoError as error:
        try:
            database = audited_mutations._majority_primary_database(repo)
            event = await database.audit.find_one({"_id": event_id, "action": "rotate_token", "targetId": identifier})
            changed = await database.tokens.find_one({"id": identifier, "tokenHash": token_hash}) if event else None
        except PyMongoError:
            changed = None
        if changed is None:
            raise HTTPException(503, "服务令牌轮换结果未知，请查询后重试") from error
    user = await repo.db.users.find_one({"id": changed["userId"]})
    return public_token(changed, user) | {"token": token}
