"""提供手动命令、执行记录、节点视图和服务令牌的受权 API。"""

from typing import Annotated

from fastapi import Depends, Query, Request, Response

from camera_logs.commands.manual_submission import submit_manual
from camera_logs.common.database import public
from camera_logs.common.models import InitialCommand, TokenCreate, TokenPatch, TokenRotate
from camera_logs.common.security import actor, authorize, authorize_owner
from camera_logs.users.service_tokens import (
    create_service_token,
    public_token,
    reveal_service_token,
    revoke_service_token,
    rotate_service_token,
)


def install_command_routes(app, repo, listing):
    """安装命令相关路由；所有写入均在鉴权后记录审计事件。"""
    User = Annotated[dict, Depends(actor)]

    @app.post("/api/v1/tasks/{task_id}/commands", status_code=202)
    async def command(task_id: str, body: InitialCommand, request: Request, user: User):
        """将手动命令加入当前采集会话队列，并限制每任务待执行数量。"""
        authorize(user, "commands:send", task_id)
        authorize_owner(user, await repo().get("tasks", task_id))
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
    async def create_token(body: TokenCreate, response: Response, user: User):
        """创建绑定用户的一次性令牌，后续权限始终跟随该用户的实时状态。"""
        authorize(user, "admin")
        response.headers["Cache-Control"] = "no-store"
        return await create_service_token(repo(), user["id"], body)

    @app.get("/api/v1/service-tokens")
    async def service_tokens(user: User, page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100)):
        """管理员读取全量，绑定用户仅读取自己的令牌公开信息。"""
        authorize(user, "service-tokens:read")
        query = {} if user.get("isAdmin") or "*" in user["scopes"] else {"userId": user["id"]}
        result = await listing("tokens", query, page, pageSize)
        users = {item["id"]: item async for item in repo().db.users.find(
            {"id": {"$in": [token.get("userId") for token in result["items"]]}}
        )}
        result["items"] = [public_token(token, users.get(token.get("userId"))) for token in result["items"]]
        return result

    @app.post("/api/v1/service-tokens/{identifier}/reveal")
    async def reveal_token(identifier: str, response: Response, user: User):
        """管理员或绑定用户查看已加密保存的服务令牌明文。"""
        authorize(user, "service-tokens:read")
        response.headers["Cache-Control"] = "no-store"
        return await reveal_service_token(repo(), user, identifier)

    @app.patch("/api/v1/service-tokens/{identifier}")
    async def edit_token(identifier: str, body: TokenPatch, user: User):
        """管理员修改令牌名称或绑定用户；下一次 Bearer 认证立即使用新用户权限。"""
        authorize(user, "admin")
        from camera_logs.users.service_tokens import update_service_token
        return await update_service_token(repo(), user["id"], identifier, body)

    @app.post("/api/v1/service-tokens/{identifier}/rotate")
    async def rotate_token(identifier: str, body: TokenRotate, response: Response, user: User):
        """管理员以版本锁生成新令牌，旧令牌在事务提交后立即失效。"""
        authorize(user, "admin")
        response.headers["Cache-Control"] = "no-store"
        return await rotate_service_token(repo(), user["id"], identifier, body.version)

    @app.delete("/api/v1/service-tokens/{identifier}", status_code=204)
    async def revoke_token(identifier: str, user: User):
        """撤销服务令牌并追加审计，不删除历史令牌记录。"""
        authorize(user, "admin")
        await revoke_service_token(repo(), user["id"], identifier)
