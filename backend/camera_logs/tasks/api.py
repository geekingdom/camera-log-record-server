"""任务配置和异步控制接口。

接口只修改期望状态，由调度器和节点完成连接操作。配置使用版本号防止覆盖，
密码留空表示保留；运行中更改连接或命令会触发受控停止再启动。
"""
import inspect
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from fastapi import Path as ApiPath
from pymongo import ReturnDocument

from camera_logs.common.audited_mutations import audited_mutation
from camera_logs.common.database import now, public
from camera_logs.common.models import BlockedRestart, TaskCreate, TaskPatch, new_id
from camera_logs.common.security import actor, authorize, authorize_owner
from camera_logs.resources.lifecycle import reconcile_task_deletion
from camera_logs.tasks.control import request_control
from camera_logs.tasks.creation import create_task as create_task_atomic
from camera_logs.tasks.editing import edit_task as edit_task_atomic
from camera_logs.tasks.recovery import request_blocked_restart
from camera_logs.tasks.resource_binding import bind_resource


def _task_sort(sort_by: str | None, sort_order: str | None) -> list[tuple[str, int]]:
    """解析任务列表排序；清除排序后回到稳定的创建时间默认顺序。"""
    fields = {"name": "name", "connection": "ip", "status": "status", "desiredState": "desiredState",
              "createdAt": "createdAt", "resourceDeletedAt": "resourceDeletedAt"}
    if sort_by is None and sort_order is None:
        return [("createdAt", -1), ("id", 1)]
    if sort_by in fields and sort_order in {"asc", "desc"}:
        direction = 1 if sort_order == "asc" else -1
        if sort_by == "connection":
            return [("ip", direction), ("port", direction), ("protocol", direction), ("id", 1)]
        return [(fields[sort_by], direction), ("id", 1)]
    raise HTTPException(422, "任务排序字段或方向无效")


async def _page_tasks(repo, query: dict, page: int, page_size: int,
                      sort_by: str | None, sort_order: str | None) -> dict:
    """在资源删除时间排序时先关联资源，保证派生列也在完整筛选集内排序。"""
    sort = _task_sort(sort_by, sort_order)
    if sort_by != "resourceDeletedAt":
        cursor = repo().db.tasks.find(query).sort(sort).skip((page - 1) * page_size).limit(page_size)
        items = [public(task) async for task in cursor]
    else:
        pipeline = [
            {"$match": query},
            {"$lookup": {"from": "resources", "localField": "resourceId", "foreignField": "id", "as": "_resources"}},
            {"$lookup": {"from": "resources", "localField": "serialServerResourceId", "foreignField": "id", "as": "_serialResources"}},
            {"$set": {"_boundResources": {"$concatArrays": ["$_resources", "$_serialResources"]}}},
            {"$set": {"_deletedResource": {"$arrayElemAt": [{"$filter": {
                "input": "$_boundResources", "as": "resource",
                "cond": {"$ne": ["$$resource.deletedAt", None]},
            }}, 0]}}},
            {"$set": {"resourceDeletedAt": {"$cond": [
                {"$eq": ["$resourceDeleted", True]}, "$_deletedResource.deletedAt", None,
            ]}}},
            {"$sort": dict(sort)},
            {"$skip": (page - 1) * page_size}, {"$limit": page_size},
            {"$project": {"_resources": 0, "_serialResources": 0, "_boundResources": 0, "_deletedResource": 0}},
        ]
        cursor = repo().db.tasks.aggregate(pipeline)
        if inspect.isawaitable(cursor):
            cursor = await cursor
        items = [public(task) async for task in cursor]
    return {"items": items, "total": await repo().db.tasks.count_documents(query), "page": page, "pageSize": page_size}


def install_task_routes(app, repo, listing):
    """注册任务路由，统一复用身份校验、分页和仓储服务。"""
    User = Annotated[dict, Depends(actor)]

    @app.get("/api/v1/tasks")
    async def tasks(user: User, page: int = Query(1, ge=1, description='从 1 开始的分页页码。'), pageSize: int = Query(20, ge=1, le=100, description='每页返回的记录数，受接口允许的最大值限制。'),
                    status: Annotated[str | None, Query(description='按采集任务的实际状态精确筛选，例如 COLLECTING（采集中）、PAUSED（已暂停）、STOPPED（已停止）或 BLOCKED（等待隔离处理）；省略不过滤。')] = None, search: Annotated[str | None, Query(description='按资源名称、地址、型号、序列号或软件版本进行综合检索。')] = None,
                    resourceId: Annotated[str | None, Query(description='按所属设备资源标识筛选采集任务或日志任务选择结果。')] = None, createdBy: Annotated[str | None, Query(description='按资源、任务或模板的创建用户标识筛选。')] = None,
                    includeDeleted: Annotated[bool, Query(description='是否包含软删除的采集任务；默认 false。true 同时返回未删除和已删除任务，历史日志仍按保留期保存。')] = False,
                    sortBy: str | None = Query(None, max_length=32, description='列表排序字段，须与sortOrder同时指定；在全部筛选结果排序后分页，相同值以稳定ID排序。省略恢复创建时间倒序。'), sortOrder: str | None = Query(None, max_length=4, description='排序方向：asc升序或desc降序；与sortBy同时指定。')):
        authorize(user, "tasks:read")
        conditions = [] if includeDeleted else [{"deletedAt": None}]
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
        result = await _page_tasks(repo, query, page, pageSize, sortBy, sortOrder)
        identifiers = {identifier for task in result["items"] for identifier in (
            task.get("resourceId"), task.get("serialServerResourceId"),
        ) if identifier}
        resources = {
            resource["id"]: resource.get("deletedAt")
            async for resource in repo().db.resources.find({"id": {"$in": list(identifiers)}}, {"id": 1, "deletedAt": 1})
        }
        for task in result["items"]:
            if "resourceDeletedAt" in task:
                continue
            if task.get("resourceDeleted"):
                deleted_at = next((resources.get(identifier) for identifier in (
                    task.get("resourceId"), task.get("serialServerResourceId"),
                ) if resources.get(identifier) is not None), None)
                if deleted_at is not None:
                    task["resourceDeletedAt"] = deleted_at
        return result

    @app.post("/api/v1/tasks", status_code=201, description='创建资源所属采集任务。SSH和Telnet设备类型仅允许当前认证成功的海康网络资源；Telnet串口不受设备HTTP认证状态限制。可用useResourceCredentials在服务端复制资源登录凭据，autoStart默认false，启动与新建均受当前账号权限约束。')
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
            binding = await bind_resource(repo(), body, require_current_auth=True)
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
    async def get_task(task_id: Annotated[str, ApiPath(description='目标采集任务 ID，可由创建任务响应或查询采集任务列表返回。')], user: User):
        authorize(user, "tasks:read", task_id)
        return public(await repo().get("tasks", task_id))

    @app.patch("/api/v1/tasks/{task_id}")
    async def edit_task(task_id: Annotated[str, ApiPath(description='目标采集任务 ID，可由创建任务响应或查询采集任务列表返回。')], body: TaskPatch, user: User):
        """提交原子配置编辑；运行语义变化会先受控停止并等待 Worker 物理收尾。"""
        return public(await edit_task_atomic(repo(), user, task_id, body))

    @app.delete("/api/v1/tasks/{task_id}", status_code=202)
    async def delete_task(task_id: Annotated[str, ApiPath(description='目标采集任务 ID，可由创建任务响应或查询采集任务列表返回。')], user: User, version: int = Query(ge=1, description='删除或更新对象时必须提供的当前版本号。')):
        """软删除任务并请求持有节点回收连接；日志、归档和下载制品继续按保留期存在。"""
        authorize(user, "tasks:write", task_id)
        authorize(user, "tasks:control", task_id)
        old = await repo().get("tasks", task_id)
        authorize_owner(user, old)
        if old.get("deletedAt") is not None:
            if old.get("version") != version:
                raise HTTPException(409, "任务版本已变化或已删除，请刷新")
            return public(await reconcile_task_deletion(repo(), task_id) or old)
        timestamp = now()

        async def commit(session):
            changed = await repo().db.tasks.find_one_and_update(
                {"id": task_id, "version": version, "deletedAt": None},
                {"$set": {"deletedAt": timestamp, "deletionState": "PENDING", "desiredState": "STOPPED",
                          "restartRequested": False, "updatedAt": timestamp}, "$inc": {"version": 1}},
                return_document=ReturnDocument.AFTER, session=session,
            )
            if changed is None:
                raise HTTPException(409, "任务版本已变化或已删除，请刷新")
            await repo().db.operations.update_many(
                {"taskId": task_id, "desiredState": {"$ne": "STOPPED"}, "status": "PENDING"},
                {"$set": {"status": "CANCELLED", "completedAt": timestamp}}, session=session,
            )
            return changed

        changed = await audited_mutation(repo(), user["id"], "delete_task", task_id, commit)
        return public(await reconcile_task_deletion(repo(), task_id) or changed)

    @app.post("/api/v1/tasks/{task_id}/start", status_code=202)
    async def start(task_id: Annotated[str, ApiPath(description='要启动的采集任务 ID，可由创建任务响应或查询采集任务列表返回。')], user: User):
        """提交启动意图，不在 API 内建立或重复建立设备连接。"""
        return public(await request_control(repo(), task_id, "RUNNING", user))

    @app.post("/api/v1/tasks/{task_id}/restart", status_code=202)
    async def restart(task_id: Annotated[str, ApiPath(description='要恢复的 BLOCKED 采集任务 ID，可由查询采集任务详情或列表返回。')], user: User, body: BlockedRestart | None = None):
        """恢复 BLOCKED 任务；外部隔离确认由管理员专用路径处理。"""
        body = body or BlockedRestart()
        if body.confirmIsolation:
            from camera_logs.node.recovery import confirm_task_isolation
            if body.evidence is None:
                raise HTTPException(422, "确认隔离必须提供证据")
            return public(await confirm_task_isolation(repo(), task_id, user, body.evidence))
        return public(await request_blocked_restart(repo(), task_id, user))

    @app.post("/api/v1/tasks/{task_id}/stop", status_code=202)
    async def stop(task_id: Annotated[str, ApiPath(description='要停止的采集任务 ID，可由创建任务响应或查询采集任务列表返回。')], user: User):
        """提交停止意图，资源已删除仍允许停止和回收原运行。"""
        return public(await request_control(repo(), task_id, "STOPPED", user))

    @app.post("/api/v1/tasks/{task_id}/pause", status_code=202)
    async def pause(task_id: Annotated[str, ApiPath(description='要暂停的 SSH 或 TELNET_DEVICE 采集任务 ID，可由查询采集任务列表返回。')], user: User):
        """请求 SSH 或 Telnet 设备关闭连接并保留预算，串口不支持暂停。"""
        return public(await request_control(repo(), task_id, "PAUSED", user))

    @app.post("/api/v1/tasks/{task_id}/resume", status_code=202)
    async def resume(task_id: Annotated[str, ApiPath(description='要继续的已暂停采集任务 ID，可由查询采集任务详情或列表返回。')], user: User):
        """在同一控制事务内验证暂停已完成，继续原运行而非重置命令预算。"""
        return public(await request_control(repo(), task_id, "RUNNING", user, require_paused=True))

    @app.get("/api/v1/operations/{identifier}")
    async def operation(identifier: Annotated[str, ApiPath(description='目标任务控制操作 ID，由启动、停止、暂停、继续、重启接口或自动启动创建响应中的 operationId 返回。')], user: User):
        doc = await repo().get("operations", identifier)
        authorize(user, "tasks:read", doc["taskId"])
        return public(doc)
