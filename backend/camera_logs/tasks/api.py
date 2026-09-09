"""任务配置和异步控制接口。

接口只修改期望状态，由调度器和节点完成连接操作。配置使用版本号防止覆盖，
密码留空表示保留；运行中更改连接或命令会触发受控停止再启动。
"""
from typing import Annotated

from fastapi import Depends, Query, Request

from camera_logs.common.database import public
from camera_logs.common.models import TaskCreate, TaskPatch, new_id
from camera_logs.common.security import actor, authorize
from camera_logs.tasks.control import request_control
from camera_logs.tasks.creation import create_task as create_task_atomic
from camera_logs.tasks.editing import edit_task as edit_task_atomic
from camera_logs.tasks.resource_binding import bind_resource


def install_task_routes(app, repo, listing):
    """注册任务路由，统一复用身份校验、分页和仓储服务。"""
    User = Annotated[dict, Depends(actor)]

    @app.get("/api/v1/tasks")
    async def tasks(user: User, page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100),
                    status: str | None = None, search: str | None = None,
                    resourceId: str | None = None, createdBy: str | None = None):
        authorize(user, "tasks:read")
        conditions = []
        if status:
            conditions.append({"status": status})
        if resourceId:
            conditions.append({"$or": [{"resourceId": resourceId}, {"serialServerResourceId": resourceId}]})
        if createdBy:
            conditions.append({"createdBy": createdBy})
        if search:
            import re
            conditions.append({"$or": [{key: {"$regex": re.escape(search), "$options": "i"}} for key in ("name", "ip")]})
        query = {} if not conditions else conditions[0] if len(conditions) == 1 else {"$and": conditions}
        result = await listing("tasks", query, page, pageSize)
        identifiers = {identifier for task in result["items"] for identifier in (
            task.get("resourceId"), task.get("serialServerResourceId"),
        ) if identifier}
        resources = {
            resource["id"]: resource.get("deletedAt")
            async for resource in repo().db.resources.find({"id": {"$in": list(identifiers)}}, {"id": 1, "deletedAt": 1})
        }
        for task in result["items"]:
            if task.get("resourceDeleted"):
                deleted_at = next((resources.get(identifier) for identifier in (
                    task.get("resourceId"), task.get("serialServerResourceId"),
                ) if resources.get(identifier) is not None), None)
                if deleted_at is not None:
                    task["resourceDeletedAt"] = deleted_at
        return result

    @app.post("/api/v1/tasks", status_code=201)
    async def create_task(body: TaskCreate, request: Request, user: User):
        """复制命令配置并分配独立命令 ID，幂等请求只能创建一个任务。"""
        authorize(user, "tasks:create")
        if body.autoStart:
            authorize(user, "tasks:control")
        async def build(identifier):
            """在确认非同键重放后校验模板来源，保留用户调整后的独立命令快照。"""
            source_version = body.sourceTemplateVersion
            if body.sourceTemplateId:
                from camera_logs.commands.templates import readable_template
                template = await readable_template(repo(), user, body.sourceTemplateId)
                if source_version is None:
                    source_version = template["version"]
            binding = await bind_resource(repo(), body)
            doc = body.model_dump()
            doc["sourceTemplateVersion"] = source_version
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
        """提交原子配置编辑；运行语义变化会先受控停止并等待 Worker 物理收尾。"""
        return public(await edit_task_atomic(repo(), user, task_id, body))

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
