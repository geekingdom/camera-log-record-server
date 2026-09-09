"""登录、密码轮换和仅管理员可用的子账户配置接口。"""

import hashlib
import logging
import re
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request, Response
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from camera_logs.access_policy.policy import enforce_ip, restrict_identity
from camera_logs.common.audited_mutations import audited_mutation
from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.common.security import actor, authorize
from camera_logs.users.models import PERMISSIONS, Login, PasswordChange, PasswordReset, UserCreate, UserPatch
from camera_logs.users.passwords import hash_password, password_work, verify_password
from camera_logs.users.session_mutations import revoke_session, rotate_session
from camera_logs.users.sessions import COOKIE, check_origin, public_user, set_session_cookie, user_identity

logger = logging.getLogger(__name__)


async def _login_budget(repo, username, address):
    """跨 API 进程共享五分钟登录预算；失败不暴露账号是否存在。"""
    instant = now()
    bucket = int(instant.timestamp()) // 300
    for kind, value, limit in (("account", username, 10), ("address", address, 100)):
        digest = hashlib.sha256(value.encode()).hexdigest()
        record = await repo.db.login_limits.find_one_and_update(
            {"_id": f"{kind}:{bucket}:{digest}"},
            {"$inc": {"attempts": 1}, "$setOnInsert": {"expiresAt": instant+timedelta(minutes=10)}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
        if record["attempts"] > limit:
            raise HTTPException(429, "登录尝试过于频繁，请稍后再试")


def install_user_routes(app):
    """安装 cookie 登录与用户管理；第三方令牌保留独立鉴权入口。"""
    User = Annotated[dict, Depends(actor)]

    def repo():
        return app.state.repo

    @app.post("/api/v1/auth/login")
    async def login(body: Login, request: Request, response: Response):
        check_origin(request, write=True)
        username = body.username.strip().lower()
        await _login_budget(repo(), username, request.client.host if request.client else "unknown")
        user = await repo().db.users.find_one({"username": username})
        if user:
            valid = await password_work(repo(), verify_password, body.password, user["passwordHash"])
        else:
            await password_work(repo(), hash_password, body.password)
            valid = False
        if not valid or not user or not user.get("enabled") or user.get("deletedAt"):
            await repo().audit("anonymous", "login_failed", None)
            raise HTTPException(401, "用户名或密码错误，或账号不可用")
        # 登录入口没有 actor 依赖，只在凭据校验成功后绑定访问日志主体。
        request.state.actor = {"id": user["id"]}
        policy_scopes = await enforce_ip(repo(), request)
        token, current = await rotate_session(repo(), user, request.cookies.get(COOKIE, ""))
        identity = public_user(restrict_identity(await user_identity(repo(), current), policy_scopes))
        set_session_cookie(repo(), token, request, response)
        return {"user": identity}

    @app.get("/api/v1/auth/me")
    async def me(user: User, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if user.get("kind") != "session":
            raise HTTPException(401, "请使用用户名和密码登录")
        return {"user": public_user(user)}

    @app.post("/api/v1/auth/logout", status_code=204)
    async def logout(request: Request, response: Response):
        check_origin(request, write=True)
        token = request.cookies.get(COOKIE, "")
        user_id = await revoke_session(repo(), token)
        response.delete_cookie(COOKIE, path="/api/v1", httponly=True, samesite="strict")
        if user_id:
            request.state.actor = {"id": user_id}

    @app.post("/api/v1/auth/password")
    async def password(body: PasswordChange, request: Request, response: Response, user: User):
        if user.get("kind") != "session":
            raise HTTPException(403, "仅登录用户可以修改自身密码")
        current = await repo().get("users", user["id"])
        if not await password_work(repo(), verify_password, body.currentPassword, current["passwordHash"]):
            raise HTTPException(400, "当前密码错误")
        if body.currentPassword == body.newPassword:
            raise HTTPException(422, "新密码不能与当前密码相同")
        password_hash = await password_work(repo(), hash_password, body.newPassword)

        policy_scopes = await enforce_ip(repo(), request)
        token, changed = await rotate_session(repo(), current, request.cookies.get(COOKIE, ""),
                                               password_hash=password_hash)
        identity = public_user(restrict_identity(await user_identity(repo(), changed), policy_scopes))
        set_session_cookie(repo(), token, request, response)
        return {"user": identity}

    @app.get("/api/v1/users/permissions")
    async def permissions(user: User):
        authorize(user, "admin")
        return {"scopes": [{"value": value, "label": label} for value, label in PERMISSIONS.items()]}

    @app.get("/api/v1/users/creators")
    async def creators(user: User, page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100),
                       search: str = Query("", max_length=128)):
        """提供历史创建人筛选所需最小目录，保留停用及删除用户但不暴露权限和凭据。"""
        if "*" not in user["scopes"] and not {"tasks:read", "templates:read"}.intersection(user["scopes"]):
            raise HTTPException(403, "无创建用户目录读取权限")
        query = {"$or": [{field: {"$regex": re.escape(search.strip()), "$options": "i"}}
                          for field in ("username", "displayName")]} if search.strip() else {}
        cursor = repo().db.users.find(query, {"id": 1, "username": 1, "displayName": 1}).sort([("username", 1), ("id", 1)])
        return {"items": [{key: item.get(key) for key in ("id", "username", "displayName")} async for item in
                          cursor.skip((page-1)*pageSize).limit(pageSize)],
                "page": page, "pageSize": pageSize, "total": await repo().db.users.count_documents(query)}

    @app.get("/api/v1/users/share-targets")
    async def share_targets(user: User, page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100)):
        """分页返回可被模板共享的有效用户最小目录，不暴露权限或会话安全字段。"""
        authorize(user, "templates:read")
        query = {"enabled": True, "deletedAt": None}
        items = repo().db.users.find(query, {"id": 1, "username": 1, "displayName": 1}).sort("username", 1)
        items = items.skip((page-1)*pageSize).limit(pageSize)
        return {"items": [{key: item.get(key) for key in ("id", "username", "displayName")} async for item in items],
                "page": page, "pageSize": pageSize, "total": await repo().db.users.count_documents(query)}

    @app.get("/api/v1/users")
    async def users(user: User, page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100)):
        authorize(user, "admin")
        query = {"deletedAt": None}
        items = repo().db.users.find(query).sort("createdAt", -1).skip((page-1)*pageSize).limit(pageSize)
        return {"items": [public_user(item) async for item in items], "page": page, "pageSize": pageSize,
                "total": await repo().db.users.count_documents(query)}

    @app.post("/api/v1/users", status_code=201)
    async def create(body: UserCreate, user: User):
        authorize(user, "admin")
        doc = body.model_dump(exclude={"password"}) | {
            "id": new_id(), "builtin": False, "version": 1, "authVersion": 1,
            "passwordHash": await password_work(repo(), hash_password, body.password),
            "mustChangePassword": True, "createdAt": now(), "updatedAt": now(),
        }

        async def commit(session):
            """账户插入与创建审计使用同一会话，重名错误不会产生成功审计。"""
            await repo().db.users.insert_one(doc, session=session)
            return doc

        try:
            await audited_mutation(repo(), user["id"], "create_user", doc["id"], commit)
        except DuplicateKeyError:
            raise HTTPException(409, "用户名已存在") from None
        return public_user(doc)

    async def update(identifier, version, updates, user, action):
        """版本控制防止覆盖；内置管理员保护不依赖前端按钮。"""
        authorize(user, "admin")
        old = await repo().get("users", identifier)
        if old.get("builtin"):
            raise HTTPException(403, "内置管理员仅能自行修改密码")
        async def commit(session):
            """账号 CAS 未命中时整体回滚，不能单独留下编辑或删除审计。"""
            changed = await repo().db.users.find_one_and_update(
                {"id": identifier, "version": version, "deletedAt": None, "builtin": False},
                {"$set": updates | {"updatedAt": now()}, "$inc": {"version": 1, "authVersion": 1}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if not changed:
                raise HTTPException(409, "账号版本已变化或已删除")
            return changed

        changed = await audited_mutation(repo(), user["id"], action, identifier, commit)
        return public_user(changed)

    @app.patch("/api/v1/users/{identifier}")
    async def edit(identifier: str, body: UserPatch, user: User):
        authorize(user, "admin")
        values = body.model_dump(exclude_unset=True, exclude={"version"})
        if "scopes" in values:
            if values["scopes"] is None or set(values["scopes"]) - PERMISSIONS.keys():
                raise HTTPException(422, "权限配置无效")
            values["scopes"] = list(dict.fromkeys(values["scopes"]))
        if "displayName" in values:
            if not values["displayName"] or not values["displayName"].strip():
                raise HTTPException(422, "显示名称不能为空")
            values["displayName"] = values["displayName"].strip()
        if "enabled" in values and values["enabled"] is None:
            raise HTTPException(422, "启用状态不能为空")
        return await update(identifier, body.version, values, user, "edit_user")

    @app.post("/api/v1/users/{identifier}/reset-password")
    async def reset(identifier: str, body: PasswordReset, user: User):
        authorize(user, "admin")
        return await update(identifier, body.version, {
            "passwordHash": await password_work(repo(), hash_password, body.password),
            "mustChangePassword": True}, user, "reset_user_password")

    @app.delete("/api/v1/users/{identifier}", status_code=204)
    async def delete(identifier: str, user: User, version: int = Query(ge=1)):
        await update(identifier, version, {"enabled": False, "deletedAt": now()}, user, "delete_user")
