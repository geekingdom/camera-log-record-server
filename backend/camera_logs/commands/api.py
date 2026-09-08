"""提供手动命令、执行记录、节点视图和服务令牌的受权 API。"""

import hashlib
import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request

from camera_logs.common.database import now, public
from camera_logs.common.models import InitialCommand, TokenCreate, new_id
from camera_logs.common.security import actor, authorize


def install_command_routes(app, repo, listing):
    """安装命令相关路由；所有写入均在鉴权后记录审计事件。"""
    User = Annotated[dict, Depends(actor)]

    @app.post("/api/v1/tasks/{task_id}/commands", status_code=202)
    async def command(task_id: str, body: InitialCommand, request: Request, user: User):
        """将手动命令加入当前采集会话队列，并限制每任务待执行数量。"""
        authorize(user, "commands:send", task_id)
        task = await repo().get("tasks", task_id)
        if task["status"] != "COLLECTING" or task["desiredState"] != "RUNNING":
            raise HTTPException(409, "任务未处于可交互采集状态")
        async def build(identifier):
            if await repo().db.commands.count_documents({"taskId": task_id, "status": "QUEUED"}) >= 100:
                raise HTTPException(429, "命令队列已满")
            doc = body.model_dump() | {"id": identifier, "taskId": task_id, "runId": task["runId"],
                "sessionId": task.get("sessionId"), "actor": user["id"], "status": "QUEUED",
                "kind": "MANUAL", "createdAt": now()}
            await repo().db.commands.insert_one(doc)
            return doc
        return public(await repo().idem(user["id"], request.headers.get("Idempotency-Key"), "command:"+task_id,
                                        body.model_dump(), "commands", build))

    @app.get("/api/v1/commands/{identifier}")
    async def get_command(identifier: str, user: User):
        """读取单个命令执行状态，并按所属任务校验读取范围。"""
        doc = await repo().get("commands", identifier)
        authorize(user, "tasks:read", doc["taskId"])
        return public(doc)

    @app.get("/api/v1/tasks/{task_id}/command-executions")
    async def executions(task_id: str, user: User, page: int = Query(1, ge=1), pageSize: int = Query(50, ge=1, le=100)):
        """分页返回任务的手动和计划命令执行记录。"""
        authorize(user, "tasks:read", task_id)
        return await listing("commands", {"taskId": task_id}, page, pageSize)

    @app.get("/api/v1/nodes")
    async def nodes(user: User):
        """返回采集节点心跳视图，要求普通任务读取权限。"""
        authorize(user, "tasks:read")
        return await listing("nodes", {}, 1, 100, "heartbeat")

    @app.post("/api/v1/service-tokens", status_code=201)
    async def create_token(body: TokenCreate, user: User):
        """创建一次性返回明文的新服务令牌，数据库仅保存散列。"""
        authorize(user, "admin")
        token = secrets.token_urlsafe(32)
        doc = {"id": new_id(), "name": body.name, "scopes": body.scopes, "taskIds": body.taskIds,
               "tokenHash": hashlib.sha256(token.encode()).hexdigest(), "revoked": False,
               "expiresAt": now()+timedelta(days=body.expiresInDays), "createdAt": now()}
        await repo().db.tokens.insert_one(doc)
        await repo().audit(user["id"], "create_token", doc["id"])
        return public(doc) | {"token": token}

    @app.delete("/api/v1/service-tokens/{identifier}", status_code=204)
    async def revoke_token(identifier: str, user: User):
        """撤销服务令牌并追加审计，不删除历史令牌记录。"""
        authorize(user, "admin")
        await repo().db.tokens.update_one({"id": identifier}, {"$set": {"revoked": True}})
        await repo().audit(user["id"], "revoke_token", identifier)
