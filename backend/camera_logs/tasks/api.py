"""任务配置和异步控制接口。

接口只修改期望状态，由调度器和节点完成连接操作。配置使用版本号防止覆盖，
密码留空表示保留；运行中更改连接或命令会触发受控停止再启动。
"""
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from pymongo import ReturnDocument

from camera_logs.common.database import now, public
from camera_logs.common.models import TaskCreate, TaskPatch, new_id
from camera_logs.common.security import actor, authorize


def install_task_routes(app, repo, listing):
    """注册任务路由，统一复用身份校验、分页和仓储服务。"""
    User = Annotated[dict, Depends(actor)]

    @app.get("/api/v1/tasks")
    async def tasks(user: User, page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100),
                    status: str | None = None, search: str | None = None, deviceId: str | None = None):
        authorize(user, "tasks:read")
        query = {}
        if user.get("taskIds") is not None:
            query["id"] = {"$in": user["taskIds"]}
        if status:
            query["status"] = status
        if deviceId:
            query["deviceId"] = deviceId
        if search:
            import re
            query["$or"] = [{key: {"$regex": re.escape(search), "$options": "i"}} for key in ("name", "ip")]
        return await listing("tasks", query, page, pageSize)

    @app.post("/api/v1/tasks", status_code=201)
    async def create_task(body: TaskCreate, request: Request, user: User):
        """复制命令配置并分配独立命令 ID，幂等请求只能创建一个任务。"""
        authorize(user, "tasks:write")
        if body.autoStart:
            authorize(user, "tasks:control")
        if user.get("taskIds") is not None:
            raise HTTPException(403, "受限账号不能创建授权范围外的新任务")
        async def build(identifier):
            doc = body.model_dump()
            password = doc.pop("password")
            auto_start = doc.pop("autoStart")
            for cmd in doc["scheduledCommands"]:
                cmd["id"] = new_id()
            doc.update(id=identifier, version=1, deviceId=body.deviceId or identifier,
                       passwordEncrypted=repo().encrypt(password), hasPassword=bool(password),
                       desiredState="STOPPED", status="STOPPED", nodeId=None,
                       createdAt=now(), updatedAt=now(), generation=0)
            await repo().db.tasks.insert_one(doc)
            if auto_start:
                operation = await change_state(identifier, "RUNNING", user)
                doc = await repo().get("tasks", identifier)
                doc["operationId"] = operation["id"]
            return doc
        result = await repo().idem(user["id"], request.headers.get("Idempotency-Key"), "create_task",
                                   body.model_dump(), "tasks", build)
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
        changed = await repo().db.tasks.find_one_and_update({"id": task_id, "version": body.version},
            {"$set": doc, "$inc": {"version": 1}}, return_document=ReturnDocument.AFTER)
        if not changed:
            raise HTTPException(409, "配置版本已变化，请刷新")
        await repo().audit(user["id"], "edit_task", task_id)
        return public(changed)

    async def change_state(task_id, desired, user):
        """记录用户控制意图并返回可轮询操作；连接释放成功前不报告暂停或停止完成。"""
        authorize(user, "tasks:control", task_id)
        task = await repo().get("tasks", task_id)
        if desired == "PAUSED" and task["protocol"] != "SSH":
            raise HTTPException(409, "只有SSH任务支持暂停")
        if desired == "PAUSED" and task["desiredState"] not in ("RUNNING", "PAUSED"):
            raise HTTPException(409, "仅运行中的任务可以暂停")
        if desired == "RUNNING" and task["status"] == "ERROR" and task.get("nodeId") is None:
            await repo().db.tasks.update_one({"id": task_id, "status": "ERROR", "nodeId": None},
                {"$set": {"status": "STOPPED", "error": None}})
        previous = await repo().db.operations.find_one({"taskId": task_id, "desiredState": desired,
                                                       "status": "PENDING"})
        if previous and task["desiredState"] == desired:
            return previous
        # 新控制意图取代尚未完成的相反意图，调用方不应永久轮询旧操作。
        await repo().db.operations.update_many(
            {"taskId": task_id, "desiredState": {"$ne": desired}, "status": "PENDING"},
            {"$set": {"status": "CANCELLED", "completedAt": now()}})
        done = (desired == "STOPPED" and task["status"] == "STOPPED") or (
            desired == "RUNNING" and task["status"] == "COLLECTING") or (desired == "PAUSED" and task["status"] == "PAUSED")
        operation = {"id": new_id(), "taskId": task_id, "desiredState": desired,
                     "status": "SUCCEEDED" if done else "PENDING", "createdAt": now()}
        await repo().db.operations.insert_one(operation)
        state_update = {"desiredState": desired, "updatedAt": now()}
        if desired == "STOPPED":
            state_update["restartRequested"] = False
            if task["status"] == "PAUSED" and task.get("nodeId") is None:
                await repo().db.endpoint_locks.delete_one({"taskId": task_id, "runId": task.get("runId")})
                await repo().db.runs.update_one({"id": task.get("runId")}, {"$set": {"endedAt": now()}})
        await repo().db.tasks.update_one({"id": task_id}, {"$set": state_update})
        await repo().audit(user["id"], "control:"+desired, task_id)
        return operation

    @app.post("/api/v1/tasks/{task_id}/start", status_code=202)
    async def start(task_id: str, user: User):
        return public(await change_state(task_id, "RUNNING", user))

    @app.post("/api/v1/tasks/{task_id}/stop", status_code=202)
    async def stop(task_id: str, user: User):
        return public(await change_state(task_id, "STOPPED", user))

    @app.post("/api/v1/tasks/{task_id}/pause", status_code=202)
    async def pause(task_id: str, user: User):
        return public(await change_state(task_id, "PAUSED", user))

    @app.post("/api/v1/tasks/{task_id}/resume", status_code=202)
    async def resume(task_id: str, user: User):
        authorize(user, "tasks:control", task_id)
        task = await repo().get("tasks", task_id)
        if task["protocol"] != "SSH" or task["status"] != "PAUSED":
            raise HTTPException(409, "任务尚未完成SSH暂停")
        return public(await change_state(task_id, "RUNNING", user))

    @app.get("/api/v1/operations/{identifier}")
    async def operation(identifier: str, user: User):
        doc = await repo().get("operations", identifier)
        authorize(user, "tasks:read", doc["taskId"])
        return public(doc)
