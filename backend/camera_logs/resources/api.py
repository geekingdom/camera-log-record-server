"""提供设备资源的认证预览、保存和受限读取接口。"""

import re
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from pymongo import ReturnDocument

from camera_logs.common.database import now, public
from camera_logs.common.security import actor, authorize, authorize_resource
from camera_logs.resources.authentication import authenticate_network_resource
from camera_logs.resources.lifecycle import reconcile_resource_deletion, task_resource_query
from camera_logs.resources.models import ResourceInput, ResourcePatch


def _resource_public(document):
    """统一移除连接密文和内部认证时间以外的敏感资源字段。"""
    return public(document)


async def _accessible_resource_ids(repo, user: dict) -> list[str] | None:
    """将受限令牌的任务白名单投影为已关联的资源 ID，避免越权读取资源。"""
    if user.get("kind") == "session":
        return user.get("resourceIds")
    task_ids = user.get("taskIds")
    if task_ids is None:
        return None
    query = {"id": {"$in": task_ids}}
    primary = await repo().db.tasks.distinct("resourceId", query)
    serial = await repo().db.tasks.distinct("serialServerResourceId", query)
    return [identifier for identifier in set(primary) | set(serial) if identifier is not None]


async def _resource_view(repo, document, user: dict | None = None):
    """补齐资源关联任务总数与活动数，便于删除前确认影响范围。"""
    item = _resource_public(document)
    task_query = task_resource_query(item["id"])
    if user and user.get("taskIds") is not None:
        task_query = {"$and": [task_query, {"id": {"$in": user["taskIds"]}}]}
    item["taskCount"] = await repo().db.tasks.count_documents(task_query)
    item["activeTaskCount"] = await repo().db.tasks.count_documents({"$and": [task_query, {"$or": [
        {"nodeId": {"$exists": True, "$ne": None}}, {"desiredState": {"$in": ["RUNNING", "PAUSED"]}},
    ]}]})
    return item


async def _verified_metadata(body: ResourceInput) -> dict[str, str]:
    """仅网络资源需要实时认证；串口服务器没有可探测的海康设备身份。"""
    if body.kind == "SERIAL_SERVER":
        return {}
    try:
        return await authenticate_network_resource(
            ip=body.ip, username=body.username, password=body.password, auth_type=body.authType,
        )
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from exc


def install_resource_routes(app, repo, listing):
    """注册资源路由，复用统一令牌鉴权、分页和审计约定。"""
    User = Annotated[dict, Depends(actor)]

    async def authenticated_preview_metadata(body: ResourceInput, user: dict, target: str) -> dict[str, str]:
        """执行网络设备认证并记录失败类别，成功事件须待调用方批准身份后写入。"""
        try:
            return await _verified_metadata(body)
        except HTTPException as exc:
            action = "authenticate_resource_credentials_rejected" if exc.status_code == 401 else \
                "authenticate_resource_device_error"
            await repo().audit(user["id"], action, target)
            raise

    @app.get("/api/v1/resources")
    async def resources(user: User, page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100),
                        kind: str | None = None, search: str | None = None, includeDeleted: bool = False):
        """按资源类型和名称或地址搜索，并在受限令牌下收窄到授权资源 ID。"""
        authorize(user, "tasks:read")
        query = {} if includeDeleted else {"deletedAt": None}
        resource_ids = await _accessible_resource_ids(repo, user)
        if resource_ids is not None:
            query["id"] = {"$in": resource_ids}
        if kind:
            query["kind"] = kind
        if search:
            query["$or"] = [{field: {"$regex": re.escape(search), "$options": "i"}} for field in ("name", "ip")]
        result = await listing("resources", query, page, pageSize)
        result["items"] = [await _resource_view(repo, item, user) for item in result["items"]]
        return result

    @app.get("/api/v1/resources/{identifier}")
    async def get_resource(identifier: str, user: User):
        """读取单个资源前按受限令牌的资源 ID 白名单授权。"""
        authorize(user, "tasks:read")
        resource_ids = await _accessible_resource_ids(repo, user)
        if resource_ids is not None and identifier not in resource_ids:
            raise HTTPException(403, "资源不在授权范围内")
        return await _resource_view(repo, await repo().get("resources", identifier), user)

    @app.post("/api/v1/resources/authenticate")
    async def authenticate_resource(body: ResourceInput, user: User):
        """返回网络设备认证预览，结果只来自本次服务端设备请求。"""
        authorize(user, "resources:create")
        if user.get("taskIds") is not None:
            raise HTTPException(403, "受限账号不能探测授权范围外的新资源")
        if body.kind == "SERIAL_SERVER":
            return {}
        metadata = await authenticated_preview_metadata(body, user, f"ip:{body.ip}")
        await repo().audit(user["id"], "authenticate_resource_succeeded", f"ip:{body.ip}")
        return metadata

    @app.post("/api/v1/resources", status_code=201)
    async def create_resource(body: ResourceInput, request: Request, user: User):
        """重新认证网络设备后保存密文凭据，客户端不能提交设备元数据。"""
        authorize(user, "resources:create")
        if user.get("taskIds") is not None:
            raise HTTPException(403, "受限账号不能创建授权范围外的新资源")
        async def build(identifier):
            metadata = await _verified_metadata(body)
            document = {"id": identifier, "name": body.name, "kind": body.kind, "ip": body.ip,
                        "version": 1, "createdAt": now(), "updatedAt": now()}
            if body.kind == "HIKVISION_NETWORK":
                document.update(username=body.username, authType=body.authType,
                                passwordEncrypted=repo().encrypt(body.password), authenticatedAt=now(), **metadata)
            await repo().db.resources.insert_one(document)
            return document

        result = await repo().idem(user["id"], request.headers.get("Idempotency-Key"), "create_resource",
                                   body.model_dump(), "resources", build)
        return await _resource_view(repo, result, user)

    @app.post("/api/v1/resources/{identifier}/authenticate")
    async def authenticate_existing_resource(identifier: str, body: ResourceInput, user: User):
        """编辑资源的认证预览只能访问该已授权资源的固定地址。"""
        authorize(user, "resources:write")
        authorize_resource(user, identifier)
        if user.get("taskIds") is not None and user.get("kind") != "session":
            raise HTTPException(403, "受限令牌不能编辑共享资源")
        old = await repo().get("resources", identifier)
        if old.get("deletedAt") or body.ip != old["ip"] or body.kind != old["kind"]:
            raise HTTPException(409, "资源地址、类型已变化或资源已删除")
        if old["kind"] == "SERIAL_SERVER":
            return {}
        metadata = await authenticated_preview_metadata(body, user, identifier)
        if old["kind"] == "HIKVISION_NETWORK" and (
            metadata["model"] != old.get("model") or metadata["subSerialNumber"] != old.get("subSerialNumber")
        ):
            await repo().audit(user["id"], "authenticate_resource_identity_changed", identifier)
            raise HTTPException(409, "认证设备身份已变化；请新建资源")
        await repo().audit(user["id"], "authenticate_resource_succeeded", identifier)
        return metadata

    @app.patch("/api/v1/resources/{identifier}")
    async def edit_resource(identifier: str, body: ResourcePatch, user: User):
        """以版本条件更新名称或 HTTP 凭据；物理 IP、类型和设备身份始终固定。"""
        authorize(user, "resources:write")
        if user.get("taskIds") is not None and user.get("kind") != "session":
            raise HTTPException(403, "受限账号不能编辑共享资源")
        authorize_resource(user, identifier)
        old = await repo().get("resources", identifier)
        if old.get("deletedAt") is not None:
            raise HTTPException(409, "资源已删除，不能编辑")
        if body.ip != old["ip"] or body.kind != old["kind"]:
            raise HTTPException(422, "资源 IP 和类型不可编辑；请新建资源")
        password = body.password or repo().decrypt(old.get("passwordEncrypted", ""))
        try:
            checked = ResourceInput(name=body.name, kind=body.kind, ip=body.ip, username=body.username,
                                    password=password, authType=body.authType)
        except ValueError as exc:
            raise HTTPException(422, "资源配置无效") from exc
        metadata = await _verified_metadata(checked)
        if checked.kind == "HIKVISION_NETWORK" and (
            metadata["model"] != old.get("model") or metadata["subSerialNumber"] != old.get("subSerialNumber")
        ):
            raise HTTPException(409, "认证设备身份已变化；请新建资源")
        update = {"name": checked.name, "updatedAt": now()}
        if checked.kind == "HIKVISION_NETWORK":
            update.update(username=checked.username, authType=checked.authType,
                          passwordEncrypted=repo().encrypt(password), authenticatedAt=now(), **metadata)
        changed = await repo().db.resources.find_one_and_update(
            {"id": identifier, "version": body.version, "deletedAt": None},
            {"$set": update, "$inc": {"version": 1}}, return_document=ReturnDocument.AFTER,
        )
        if not changed:
            raise HTTPException(409, "资源版本已变化或已删除，请刷新")
        await repo().audit(user["id"], "edit_resource", identifier)
        return await _resource_view(repo, changed, user)

    @app.delete("/api/v1/resources/{identifier}", status_code=202)
    async def delete_resource(identifier: str, user: User, version: int = Query(ge=1)):
        """软删除资源并请求所有关联任务受控停止，保留任务、文件和历史目录。"""
        authorize(user, "resources:write")
        authorize(user, "tasks:control")
        if user.get("taskIds") is not None and user.get("kind") != "session":
            raise HTTPException(403, "受限账号不能删除共享资源")
        authorize_resource(user, identifier)
        if user.get("taskIds") is not None:
            # 删除串口服务器也会停止关联网络设备任务，禁止波及范围外设备。
            outside = {"$and": [task_resource_query(identifier), {"id": {"$nin": user["taskIds"]}}]}
            if await repo().db.tasks.find_one(outside):
                raise HTTPException(403, "资源关联了授权范围外的任务，不能删除")
        old = await repo().get("resources", identifier)
        if old.get("deletedAt") is not None:
            result = await reconcile_resource_deletion(repo(), identifier)
            return await _resource_view(repo, result or old, user)
        changed = await repo().db.resources.find_one_and_update(
            {"id": identifier, "version": version, "deletedAt": None},
            {"$set": {"deletedAt": now(), "deletionState": "PENDING", "updatedAt": now()}, "$inc": {"version": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if not changed:
            raise HTTPException(409, "资源版本已变化，请刷新")
        completed = await reconcile_resource_deletion(repo(), identifier)
        await repo().audit(user["id"], "delete_resource", identifier)
        return await _resource_view(repo, completed or changed, user)
