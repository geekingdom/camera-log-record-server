"""登录、密码轮换和仅管理员可用的子账户配置接口。"""

import hashlib
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request, Response
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from camera_logs.access_policy.policy import apply_ip_permissions
from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.common.security import actor, authorize
from camera_logs.users.models import PERMISSIONS, Login, PasswordChange, PasswordReset, UserCreate, UserPatch
from camera_logs.users.passwords import hash_password, password_work, verify_password
from camera_logs.users.sessions import COOKIE, check_origin, issue_session, public_user


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


async def _resources_exist(repo, resource_ids):
    """管理员只能分配已存在资源，删除资源仍可分配以便历史日志查询。"""
    if resource_ids is not None:
        identifiers = list(dict.fromkeys(resource_ids))
        if await repo.db.resources.count_documents({"id": {"$in": identifiers}}) != len(identifiers):
            raise HTTPException(422, "授权资源不存在")


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
        # 已有浏览器会话在登录时轮换，旧凭证不得继续使用。
        old = request.cookies.get(COOKIE, "")
        if old:
            await repo().db.user_sessions.delete_one({"tokenHash": hashlib.sha256(old.encode()).hexdigest()})
        await issue_session(repo(), user, request, response)
        await repo().audit(user["id"], "login", user["id"])
        return {"user": public_user(await apply_ip_permissions(repo(), request, user))}

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
        session = await repo().db.user_sessions.find_one_and_delete({
            "tokenHash": hashlib.sha256(token.encode()).hexdigest()})
        response.delete_cookie(COOKIE, path="/api/v1", httponly=True, samesite="strict")
        if session:
            await repo().audit(session["userId"], "logout", session["userId"])

    @app.post("/api/v1/auth/password")
    async def password(body: PasswordChange, request: Request, response: Response, user: User):
        if user.get("kind") != "session":
            raise HTTPException(403, "仅登录用户可以修改自身密码")
        current = await repo().get("users", user["id"])
        if not await password_work(repo(), verify_password, body.currentPassword, current["passwordHash"]):
            raise HTTPException(400, "当前密码错误")
        if body.currentPassword == body.newPassword:
            raise HTTPException(422, "新密码不能与当前密码相同")
        changed = await repo().db.users.find_one_and_update(
            {"id": user["id"], "authVersion": current["authVersion"], "enabled": True},
            {"$set": {"passwordHash": await password_work(repo(), hash_password, body.newPassword),
                      "mustChangePassword": False, "updatedAt": now()},
             "$inc": {"version": 1, "authVersion": 1}}, return_document=ReturnDocument.AFTER,
        )
        if not changed:
            raise HTTPException(409, "账号已变化，请重新登录")
        await issue_session(repo(), changed, request, response)
        await repo().audit(user["id"], "change_password", user["id"])
        return {"user": public_user(await apply_ip_permissions(repo(), request, changed))}

    @app.get("/api/v1/users/permissions")
    async def permissions(user: User):
        authorize(user, "admin")
        return {"scopes": [{"value": value, "label": label} for value, label in PERMISSIONS.items()]}

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
        await _resources_exist(repo(), body.resourceIds)
        doc = body.model_dump(exclude={"password"}) | {
            "id": new_id(), "builtin": False, "version": 1, "authVersion": 1,
            "passwordHash": await password_work(repo(), hash_password, body.password),
            "mustChangePassword": True, "createdAt": now(), "updatedAt": now(),
        }
        try:
            await repo().db.users.insert_one(doc)
        except DuplicateKeyError:
            raise HTTPException(409, "用户名已存在") from None
        await repo().audit(user["id"], "create_user", doc["id"])
        return public_user(doc)

    async def update(identifier, version, updates, user, action):
        """版本控制防止覆盖；内置管理员保护不依赖前端按钮。"""
        authorize(user, "admin")
        old = await repo().get("users", identifier)
        if old.get("builtin"):
            raise HTTPException(403, "内置管理员仅能自行修改密码")
        changed = await repo().db.users.find_one_and_update(
            {"id": identifier, "version": version, "deletedAt": None, "builtin": False},
            {"$set": updates | {"updatedAt": now()}, "$inc": {"version": 1, "authVersion": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if not changed:
            raise HTTPException(409, "账号版本已变化或已删除")
        await repo().audit(user["id"], action, identifier)
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
        await _resources_exist(repo(), values.get("resourceIds"))
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
