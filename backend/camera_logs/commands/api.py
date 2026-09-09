"""提供手动命令、执行记录、节点视图和服务令牌的受权 API。"""

from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request

from camera_logs.commands.manual_submission import submit_manual
from camera_logs.common.database import public
from camera_logs.common.models import InitialCommand, TokenCreate
from camera_logs.common.security import actor, authorize
from camera_logs.users.service_tokens import create_service_token, revoke_service_token

SERVICE_TOKEN_SCOPES = frozenset({
    "admin", "commands:send", "logs:download", "logs:read", "tasks:control",
    "tasks:read", "tasks:write", "templates:read", "templates:write",
    "resources:create", "resources:write", "tasks:create",
})


def install_command_routes(app, repo, listing):
    """安装命令相关路由；所有写入均在鉴权后记录审计事件。"""
    User = Annotated[dict, Depends(actor)]

    @app.post("/api/v1/tasks/{task_id}/commands", status_code=202)
    async def command(task_id: str, body: InitialCommand, request: Request, user: User):
        """将手动命令加入当前采集会话队列，并限制每任务待执行数量。"""
        authorize(user, "commands:send", task_id)
        return public(await submit_manual(repo(), user["id"], task_id,
                                          request.headers.get("Idempotency-Key"), body.model_dump()))

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
        """基础设施节点列表仅管理员可读取。"""
        authorize(user, "admin")
        return await listing("nodes", {"deletedAt": None}, 1, 100, "heartbeat")

    @app.post("/api/v1/service-tokens", status_code=201)
    async def create_token(body: TokenCreate, user: User):
        """创建一次性返回明文的新服务令牌，数据库仅保存散列。"""
        authorize(user, "admin")
        unknown_scopes = set(body.scopes) - SERVICE_TOKEN_SCOPES
        if unknown_scopes:
            raise HTTPException(422, f"存在不支持的权限：{', '.join(sorted(unknown_scopes))}")
        return await create_service_token(repo(), user["id"], body)

    @app.get("/api/v1/service-tokens")
    async def service_tokens(user: User, page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100)):
        """分页返回服务账号元数据；统一公开化处理确保散列永不离开服务端。"""
        authorize(user, "admin")
        return await listing("tokens", {}, page, pageSize)

    @app.delete("/api/v1/service-tokens/{identifier}", status_code=204)
    async def revoke_token(identifier: str, user: User):
        """撤销服务令牌并追加审计，不删除历史令牌记录。"""
        authorize(user, "admin")
        await revoke_service_token(repo(), user["id"], identifier)
