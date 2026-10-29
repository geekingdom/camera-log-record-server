"""登录、密码轮换和仅管理员可用的子账户配置接口。"""

import hashlib
import logging
import re
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query, Request, Response
from fastapi import Path as ApiPath
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from camera_logs.access_policy.policy import enforce_ip, restrict_identity
from camera_logs.common.audited_mutations import audited_mutation
from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.common.security import actor, authorize
from camera_logs.resources.lifecycle import mark_resource_deletion, reconcile_resource_deletion
from camera_logs.users.models import PERMISSIONS, Login, PasswordChange, PasswordReset, UserCreate, UserPatch
from camera_logs.users.passwords import hash_password, password_work, verify_password
from camera_logs.users.roles import FIXED_SCOPES, authorize_user_target, claim_account_manager
from camera_logs.users.session_mutations import revoke_session, rotate_session
from camera_logs.users.sessions import (
    COOKIE,
    check_origin,
    public_current_user,
    public_user,
    set_session_cookie,
    user_identity,
)
from camera_logs.users.write_gate import claim_active_user_for_resource_write

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


async def _record_login_failure(repo, submitted_username: str) -> None:
    """记录认证失败的提交账号；主体保持匿名且绝不携带密码或请求正文。"""
    await repo.audit("anonymous", "login_failed", None, metadata={"username": submitted_username})


def install_user_routes(app):
    """安装 cookie 登录与用户管理；第三方令牌保留独立鉴权入口。"""
    User = Annotated[dict, Depends(actor)]

    def repo():
        return app.state.repo

    @app.post("/api/v1/auth/login")
    async def login(body: Login, request: Request, response: Response):
        check_origin(request, write=True)
        username = body.username.strip().lower()
        try:
            await _login_budget(repo(), username, request.client.host if request.client else "unknown")
        except HTTPException as error:
            if error.status_code == 429:
                await _record_login_failure(repo(), body.username)
            raise
        # 软删除账号保留历史归属且允许同名新建；登录只选择当前活动身份，
        # 不能让旧文档抢先匹配并把新账号误判为不可用。
        user = await repo().db.users.find_one({"username": username, "deletedAt": None})
        if user:
            valid = await password_work(repo(), verify_password, body.password, user["passwordHash"])
        else:
            await password_work(repo(), hash_password, body.password)
            valid = False
        if not valid or not user or not user.get("enabled") or user.get("deletedAt"):
            await _record_login_failure(repo(), body.username)
            raise HTTPException(401, "用户名或密码错误，或账号不可用")
        # 登录入口没有 actor 依赖，只在凭据校验成功后绑定访问日志主体。
        request.state.actor = {"id": user["id"]}
        policy_scopes = await enforce_ip(repo(), request)
        token, current = await rotate_session(repo(), user, request.cookies.get(COOKIE, ""))
        identity = public_current_user(restrict_identity(await user_identity(repo(), current), policy_scopes))
        set_session_cookie(repo(), token, request, response)
        return {"user": identity}

    @app.get("/api/v1/auth/me")
    async def me(user: User, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if user.get("kind") != "session":
            raise HTTPException(401, "请使用用户名和密码登录")
        return {"user": public_current_user(user)}

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
        identity = public_current_user(restrict_identity(await user_identity(repo(), changed), policy_scopes))
        set_session_cookie(repo(), token, request, response)
        return {"user": identity}

    @app.get("/api/v1/users/permissions")
    async def permissions(user: User):
        authorize(user, "admin")
        return {"scopes": [{"value": value, "label": label, "fixed": value in FIXED_SCOPES} for value, label in PERMISSIONS.items()]}

    @app.get("/api/v1/users/creators")
    async def creators(user: User, page: int = Query(1, ge=1, description='从 1 开始的分页页码。'), pageSize: int = Query(20, ge=1, le=100, description='每页返回的记录数，受接口允许的最大值限制。'),
                       search: str = Query("", max_length=128, description='按资源名称、地址、型号、序列号或软件版本进行综合检索。')):
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
    async def share_targets(user: User, page: int = Query(1, ge=1, description='从 1 开始的分页页码。'), pageSize: int = Query(20, ge=1, le=100, description='每页返回的记录数，受接口允许的最大值限制。')):
        """分页返回可被资源或模板共享、接收资源转移的有效用户最小目录，不暴露权限或会话安全字段。"""
        if not any(scope in user["scopes"] for scope in ("*", "admin", "templates:read", "resources:create", "resources:write")):
            raise HTTPException(403, "权限不足")
        query = {"enabled": True, "deletedAt": None}
        items = repo().db.users.find(query, {"id": 1, "username": 1, "displayName": 1}).sort("username", 1)
        items = items.skip((page-1)*pageSize).limit(pageSize)
        return {"items": [{key: item.get(key) for key in ("id", "username", "displayName")} async for item in items],
                "page": page, "pageSize": pageSize, "total": await repo().db.users.count_documents(query)}

    @app.get("/api/v1/users")
    async def users(user: User, page: int = Query(1, ge=1, description='从 1 开始的分页页码。'), pageSize: int = Query(20, ge=1, le=100, description='每页返回的记录数，受接口允许的最大值限制。')):
        authorize(user, "admin")
        query = {"deletedAt": None}
        items = repo().db.users.find(query).sort("createdAt", -1).skip((page-1)*pageSize).limit(pageSize)
        return {"items": [public_user(item) async for item in items], "page": page, "pageSize": pageSize,
                "total": await repo().db.users.count_documents(query)}

    @app.post("/api/v1/users", status_code=201)
    async def create(body: UserCreate, user: User):
        authorize(user, "admin")
        doc = body.model_dump(exclude={"password", "isAdmin"}) | {
            "isAdmin": body.role == "ADMIN",
            "id": new_id(), "builtin": False, "version": 1, "authVersion": 1,
            "passwordHash": await password_work(repo(), hash_password, body.password),
            "mustChangePassword": True, "createdAt": now(), "updatedAt": now(),
        }

        async def commit(session):
            """账户插入与创建审计使用同一会话，重名错误不会产生成功审计。"""
            await claim_account_manager(repo(), user, session=session)
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
        authorize_user_target(user, old)
        async def commit(session):
            """账号 CAS 未命中时整体回滚，不能单独留下编辑或删除审计。"""
            current_target = await repo().db.users.find_one({"id": identifier}, session=session)
            await claim_account_manager(repo(), user, session=session, target=current_target or old)
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
    async def edit(identifier: Annotated[str, ApiPath(description='要编辑的平台用户 ID，可由创建用户响应、用户列表或创建人列表返回。')], body: UserPatch, user: User):
        authorize(user, "admin")
        values = body.model_dump(exclude_unset=True, exclude={"version"})
        if "role" in values:
            if values["role"] is None:
                raise HTTPException(422, "角色不能为空")
            values["isAdmin"] = values["role"] == "ADMIN"
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
    async def reset(identifier: Annotated[str, ApiPath(description='要重置密码的平台用户 ID，可由创建用户响应、用户列表或创建人列表返回。')], body: PasswordReset, user: User):
        authorize(user, "admin")
        return await update(identifier, body.version, {
            "passwordHash": await password_work(repo(), hash_password, body.password),
            "mustChangePassword": True}, user, "reset_user_password")

    @app.delete("/api/v1/users/{identifier}", status_code=204)
    async def delete(identifier: Annotated[str, ApiPath(description='要停用并软删除的平台用户 ID，可由创建用户响应、用户列表或创建人列表返回。')], user: User,
                     version: int = Query(ge=1, description='删除或更新对象时必须提供的当前版本号。'),
                     resourceAction: Annotated[Literal["DELETE", "TRANSFER"] | None, Query(description='删除用户已有活动设备资源时的处置方式：DELETE软删除资源并收尾关联任务；TRANSFER转移资源及该用户创建的关联任务。无活动资源时可省略。')] = None,
                     targetUserId: Annotated[str | None, Query(max_length=128, description='resourceAction 为 TRANSFER 时的接收用户 ID；必须是不同于被删用户的正常有效用户。')] = None):
        """软删除用户，并在同一事务中处置其活动资源和受影响任务。"""
        authorize(user, "admin")
        old = await repo().get("users", identifier)
        authorize_user_target(user, old)
        timestamp = now()
        deleted_resources: list[str] = []

        async def commit(session):
            """用户 CAS、资源归属变更与任务停止意图必须共同提交。"""
            nonlocal deleted_resources
            current_target = await repo().db.users.find_one({"id": identifier}, session=session)
            await claim_account_manager(repo(), user, session=session, target=current_target or old)
            resources = [item async for item in repo().db.resources.find(
                {"createdBy": identifier, "deletedAt": None}, {"id": 1}, session=session,
            )]
            if resources and resourceAction is None:
                raise HTTPException(422, "用户仍有活动设备资源，必须选择删除或转移")
            target = None
            if resourceAction == "TRANSFER":
                if not targetUserId or targetUserId == identifier:
                    raise HTTPException(422, "转移目标必须是不同于被删用户的有效用户")
                target = await repo().db.users.find_one(
                    {"id": targetUserId, "enabled": True, "deletedAt": None}, session=session,
                )
                if target is None:
                    raise HTTPException(422, "转移目标用户不存在、已停用或已删除")
                # 领取与目标删除共用的用户围栏；转移提交时目标仍须有效，
                # 不能把资源交给并发已删除的账户。
                await claim_active_user_for_resource_write(repo().db, targetUserId, session)
            if resourceAction == "DELETE":
                for resource in resources:
                    await mark_resource_deletion(repo(), resource["id"], session, timestamp)
                    await repo().audit(user["id"], "delete_user_resource", resource["id"], session=session,
                                       metadata={"createdBy": identifier})
                deleted_resources = [resource["id"] for resource in resources]
            elif resourceAction == "TRANSFER" and target is not None:
                resource_ids = [resource["id"] for resource in resources]
                if resource_ids:
                    target_name = target.get("displayName") or target.get("username") or target["id"]
                    transferable_task_ids = [task["id"] async for task in repo().db.tasks.find(
                        {"createdBy": identifier, "deletedAt": None, "$or": [
                            {"resourceId": {"$in": resource_ids}},
                            {"serialServerResourceId": {"$in": resource_ids}},
                        ]}, {"id": 1}, session=session,
                    )]
                    await repo().db.resources.update_many(
                        {"id": {"$in": resource_ids}, "createdBy": identifier, "deletedAt": None},
                        {"$set": {"createdBy": target["id"], "createdByName": target_name, "updatedAt": timestamp},
                         "$inc": {"version": 1}}, session=session,
                    )
                    for resource_id in resource_ids:
                        await repo().audit(user["id"], "transfer_user_resource", resource_id, session=session,
                                           metadata={"createdBy": identifier, "targetUserId": target["id"]})
                    # 只转移被删除用户创建的关联任务，其他创建人的任务维持独立归属。
                    await repo().db.tasks.update_many(
                        {"createdBy": identifier, "deletedAt": None, "$or": [
                            {"resourceId": {"$in": resource_ids}}, {"serialServerResourceId": {"$in": resource_ids}},
                        ]},
                        {"$set": {"createdBy": target["id"], "createdByName": target_name, "updatedAt": timestamp},
                         "$inc": {"version": 1}}, session=session,
                    )
                    for task_id in transferable_task_ids:
                        await repo().audit(user["id"], "transfer_user_task", task_id, session=session,
                                           metadata={"createdBy": identifier, "targetUserId": target["id"]})
            changed = await repo().db.users.find_one_and_update(
                {"id": identifier, "version": version, "deletedAt": None, "builtin": False},
                {"$set": {"enabled": False, "deletedAt": timestamp, "updatedAt": timestamp},
                 "$inc": {"version": 1, "authVersion": 1, "resourceWriteVersion": 1}},
                return_document=ReturnDocument.AFTER, session=session,
            )
            if changed is None:
                raise HTTPException(409, "账号版本已变化或已删除")
            return changed

        await audited_mutation(repo(), user["id"], "delete_user", identifier, commit)
        for resource_id in deleted_resources:
            try:
                await reconcile_resource_deletion(repo(), resource_id)
            except Exception:
                logger.exception("用户删除后的资源收尾失败 resource=%s", resource_id)
