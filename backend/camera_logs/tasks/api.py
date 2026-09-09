"""任务配置和异步控制接口。

接口只修改期望状态，由调度器和节点完成连接操作。配置使用版本号防止覆盖，
密码留空表示保留；运行中更改连接或命令会触发受控停止再启动。
"""
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from pymongo import ReturnDocument

from camera_logs.common.database import now, public
from camera_logs.common.models import TaskCreate, TaskPatch, new_id
from camera_logs.common.security import actor, authorize, authorize_resource
from camera_logs.tasks.control import request_control
from camera_logs.tasks.creation import create_task as create_task_atomic
from camera_logs.tasks.resource_binding import bind_resource


def install_task_routes(app, repo, listing):
    """注册任务路由，统一复用身份校验、分页和仓储服务。"""
    User = Annotated[dict, Depends(actor)]

    @app.get("/api/v1/tasks")
    async def tasks(user: User, page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100),
                    status: str | None = None, search: str | None = None,
                    resourceId: str | None = None):
        authorize(user, "tasks:read")
        query = {}
        if user.get("taskIds") is not None:
            query["id"] = {"$in": user["taskIds"]}
        if status:
            query["status"] = status
        if resourceId:
            query["$and"] = [{"$or": [{"resourceId": resourceId}, {"serialServerResourceId": resourceId}]}]
        if search:
            import re
            query["$or"] = [{key: {"$regex": re.escape(search), "$options": "i"}} for key in ("name", "ip")]
        return await listing("tasks", query, page, pageSize)

    @app.post("/api/v1/tasks", status_code=201)
    async def create_task(body: TaskCreate, request: Request, user: User):
        """复制命令配置并分配独立命令 ID，幂等请求只能创建一个任务。"""
        authorize(user, "tasks:create")
        if body.autoStart:
            authorize(user, "tasks:control")
        if user.get("taskIds") is not None and user.get("kind") != "session":
            raise HTTPException(403, "受限账号不能创建授权范围外的新任务")
        for resource_id in filter(None, (body.resourceId, body.serialServerResourceId)):
            authorize_resource(user, resource_id)
        async def build(identifier):
            """在事务外完成可能读取资源和加密的准备，返回固定任务文档。"""
            binding = await bind_resource(repo(), body)
            doc = body.model_dump()
            password = doc.pop("password")
            auto_start = doc.pop("autoStart")
            for cmd in doc["scheduledCommands"]:
                cmd["id"] = new_id()
            doc.update(id=identifier, version=1,
                       passwordEncrypted=repo().encrypt(password), hasPassword=bool(password))
            doc.update(binding)
            return doc, auto_start
        result = await create_task_atomic(repo(), user, request.headers.get("Idempotency-Key"), body.model_dump(), build)
        return public(result)

    @app.get("/api/v1/tasks/{task_id}")
    async def get_task(task_id: str, user: User):
        authorize(user, "tasks:read", task_id)
        return public(await repo().get("tasks", task_id))

    @app.patch("/api/v1/tasks/{task_id}")
    async def edit_task(task_id: str, body: TaskPatch, user: User):
        """合并并重新校验完整配置，以版本条件原子提交；暂停期间禁止改变运行语义。"""
        authorize(user, "tasks:write", task_id)
        old = await repo().get("tasks", task_id)
        if old["status"] in ("PAUSED", "PAUSING") and set(body.model_fields_set) - {"version", "name", "description"}:
            raise HTTPException(409, "暂停期间只能修改名称和说明；修改连接或命令前请停止任务")
        updates = body.model_dump(exclude_unset=True)
        updates.pop("version")
        clear = updates.pop("clearPassword", False)
        if updates.get("password") == "":
            updates.pop("password")
        base = {key: old[key] for key in TaskCreate.model_fields if key in old}
        base["password"] = repo().decrypt(old["passwordEncrypted"])
        base.update(updates)
        if clear:
            base["password"] = ""
        try:
            checked = TaskCreate(**base)
        except ValueError as exc:
            raise HTTPException(422, "任务配置无效，请检查协议、账号和命令字段") from exc
        doc = checked.model_dump(exclude={"autoStart", "password"})
        for resource_id in filter(None, (checked.resourceId, checked.serialServerResourceId)):
            authorize_resource(user, resource_id)
        doc.update(await bind_resource(repo(), checked))
        if "scheduledCommands" in updates:
            for cmd in doc["scheduledCommands"]:
                cmd["id"] = new_id()
        doc.update(passwordEncrypted=repo().encrypt(checked.password), hasPassword=bool(checked.password),
                   updatedAt=now())
        behavioral = set(updates) - {"name", "description"}
        restarting = old["desiredState"] == "RUNNING" and bool(behavioral or clear)
        if restarting:
            authorize(user, "tasks:control", task_id)
            doc.update(desiredState="STOPPED", restartRequested=True)
        changed = await repo().db.tasks.find_one_and_update(
            {"id": task_id, "version": body.version, "resourceDeleted": {"$ne": True}},
            {"$set": doc, "$inc": {"version": 1}}, return_document=ReturnDocument.AFTER)
        if not changed:
            raise HTTPException(409, "配置版本已变化或设备资源已删除，请刷新")
        await repo().audit(user["id"], "edit_task", task_id)
        return public(changed)

    @app.post("/api/v1/tasks/{task_id}/start", status_code=202)
    async def start(task_id: str, user: User):
        """提交启动意图，不在 API 内建立或重复建立设备连接。"""
        return public(await request_control(repo(), task_id, "RUNNING", user))

    @app.post("/api/v1/tasks/{task_id}/stop", status_code=202)
    async def stop(task_id: str, user: User):
        """提交停止意图，资源已删除仍允许停止和回收原运行。"""
        return public(await request_control(repo(), task_id, "STOPPED", user))

    @app.post("/api/v1/tasks/{task_id}/pause", status_code=202)
    async def pause(task_id: str, user: User):
        """请求 SSH 连接关闭并保留运行预算，连接关闭前返回待完成操作。"""
        return public(await request_control(repo(), task_id, "PAUSED", user))

    @app.post("/api/v1/tasks/{task_id}/resume", status_code=202)
    async def resume(task_id: str, user: User):
        """在同一控制事务内验证暂停已完成，继续原运行而非重置命令预算。"""
        return public(await request_control(repo(), task_id, "RUNNING", user, require_paused=True))

    @app.get("/api/v1/operations/{identifier}")
    async def operation(identifier: str, user: User):
        doc = await repo().get("operations", identifier)
        authorize(user, "tasks:read", doc["taskId"])
        return public(doc)
