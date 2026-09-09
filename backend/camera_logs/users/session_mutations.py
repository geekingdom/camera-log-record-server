"""以同一事务提交登录、退出和改密会话；提交不明时只读核实固定证据。"""

import hashlib
import logging
import secrets
from datetime import timedelta

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.models import new_id

logger = logging.getLogger(__name__)


def token_digest(token):
    """数据库只使用令牌摘要，原始 Cookie 不进入记录或诊断日志。"""
    return hashlib.sha256(token.encode()).hexdigest()


def prepare_session(repo, user):
    """在事务回调外固定随机凭证和到期时间，重试不得另生成会话。"""
    token = secrets.token_urlsafe(48)
    timestamp = now()
    document = {"tokenHash": token_digest(token), "userId": user["id"],
                "authVersion": user["authVersion"], "createdAt": timestamp,
                "expiresAt": timestamp + timedelta(seconds=repo.settings.session_seconds)}
    return token, document


async def _recover_session(repo, document, event_id):
    """仅当同一次事务的会话、审计及当前有效账号均可多数确认时恢复成功。"""
    database = audited_mutations._majority_primary_database(repo)
    try:
        stored = await database.user_sessions.find_one({
            "tokenHash": document["tokenHash"], "userId": document["userId"],
            "authVersion": document["authVersion"], "expiresAt": {"$gt": now()},
        })
        if stored and await database.audit.find_one({"_id": event_id}):
            return await database.users.find_one({
                "id": document["userId"], "authVersion": document["authVersion"],
                "enabled": True, "deletedAt": None,
            })
    except PyMongoError:
        pass
    return None


async def rotate_session(repo, verified_user, old_token, *, password_hash=None):
    """登录或改密：账号 CAS、旧会话撤销、新会话与审计原子提交。

    密码验证和新密码散列必须由调用方在事务外完成。账号声明使用实际写入，
    从而与并发停用、权限修改、密码重置产生写冲突，不能依赖只读快照。
    """
    expected_version = verified_user["authVersion"] + (password_hash is not None)
    token, document = prepare_session(repo, verified_user | {"authVersion": expected_version})
    event_id = new_id()
    action = "change_password" if password_hash is not None else "login"

    async def commit(session):
        updates = {"$inc": {"sessionClaimVersion": 1}}
        if password_hash is not None:
            updates["$inc"].update(version=1, authVersion=1)
            updates["$set"] = {"passwordHash": password_hash, "mustChangePassword": False, "updatedAt": now()}
        current = await repo.db.users.find_one_and_update(
            {"id": verified_user["id"], "authVersion": verified_user["authVersion"],
             "passwordHash": verified_user["passwordHash"], "enabled": True, "deletedAt": None},
            updates, return_document=ReturnDocument.AFTER, session=session,
        )
        if current is None:
            raise HTTPException(409, "账号已变化，请重新登录")
        if password_hash is not None:
            # 依赖鉴权与本事务之间可能发生退出/撤销；必须实际删除仍有效的原会话。
            removed = await repo.db.user_sessions.find_one_and_delete({
                "tokenHash": token_digest(old_token), "userId": current["id"],
                "authVersion": verified_user["authVersion"], "expiresAt": {"$gt": now()},
            }, session=session)
            if removed is None:
                raise HTTPException(409, "原登录会话已变化，请重新登录")
        elif old_token:
            # 仅轮换本次登录账号的旧会话，不撤销浏览器可能携带的另一账号会话。
            await repo.db.user_sessions.delete_one({"tokenHash": token_digest(old_token),
                                                     "userId": current["id"]}, session=session)
        await repo.db.user_sessions.insert_one(document, session=session)
        await repo.audit(current["id"], action, current["id"], session=session, event_id=event_id)
        return current

    try:
        current = await audited_mutations.mutation_transaction(repo, commit)
    except PyMongoError as error:
        current = await _recover_session(repo, document, event_id)
        if current is None:
            logger.warning("会话事务提交结果未知 action=%s user=%s", action, verified_user["id"])
            raise HTTPException(503, "登录或改密提交结果未知，请重新登录确认；未签发新会话") from error
    return token, current


async def revoke_session(repo, token):
    """退出会话及其审计原子提交；无匹配会话时幂等成功且不产生虚假成功审计。"""
    digest, event_id = token_digest(token), new_id()

    async def commit(session):
        removed = await repo.db.user_sessions.find_one_and_delete({"tokenHash": digest}, session=session)
        if removed is not None:
            await repo.audit(removed["userId"], "logout", removed["userId"],
                             session=session, event_id=event_id)
            return removed["userId"]
        return None

    try:
        return await audited_mutations.mutation_transaction(repo, commit)
    except PyMongoError as error:
        database = audited_mutations._majority_primary_database(repo)
        try:
            event = await database.audit.find_one({"_id": event_id})
            if event and not await database.user_sessions.find_one({"tokenHash": digest}):
                return event["actor"]
        except PyMongoError:
            pass
        logger.warning("退出会话提交结果未知")
        raise HTTPException(503, "退出提交结果未知，请重试退出；未清除浏览器凭证") from error
