"""处理 Bearer Token 身份识别、作用域校验和任务范围限制。"""

import hashlib
import secrets

from fastapi import HTTPException, Request

from camera_logs.access_policy.policy import apply_ip_permissions
from camera_logs.common.database import now
from camera_logs.users.sessions import COOKIE, check_origin, required_login, session_identity


async def authenticate(repo, token):
    """校验 bootstrap 或持久化令牌，过期和撤销令牌统一返回 401。"""
    if token and repo.settings.bootstrap_token and secrets.compare_digest(token, repo.settings.bootstrap_token):
        return {"id": "bootstrap", "scopes": ["*"], "taskIds": None}
    digest = hashlib.sha256(token.encode()).hexdigest()
    record = await repo.db.tokens.find_one({"tokenHash": digest, "revoked": False})
    if not record or record["expiresAt"].replace(tzinfo=now().tzinfo) <= now():
        raise required_login()
    # 原有第三方 tasks:write 合同包含创建资源与任务；先展开再交由 IP 策略求交集。
    if "tasks:write" in record["scopes"]:
        record = record | {"scopes": list(set(record["scopes"]) | {
            "resources:create", "resources:write", "tasks:create"})}
    return record


async def actor(request: Request):
    """从 Authorization 提取身份并写入 request.state，供审计和访问日志复用。"""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        identity = await authenticate(request.app.state.repo, auth[7:])
    elif auth:
        raise required_login()
    else:
        check_origin(request, write=request.method not in {"GET", "HEAD", "OPTIONS"})
        identity = await session_identity(request.app.state.repo, request.cookies.get(COOKIE, ""))
        if identity.get("mustChangePassword") and request.url.path not in {
            "/api/v1/auth/me", "/api/v1/auth/password", "/api/v1/auth/logout"
        }:
            raise HTTPException(403, "请先修改初始密码")
    identity = await apply_ip_permissions(request.app.state.repo, request, identity)
    request.state.actor = identity
    return identity


def authorize(identity, scope, task_id=None):
    """同时验证作用域和可选任务白名单，权限不足统一返回 403。"""
    if "*" not in identity["scopes"] and scope not in identity["scopes"]:
        raise HTTPException(403, "权限不足")
    allowed = identity.get("taskIds")
    if task_id is not None and allowed is not None and task_id not in allowed:
        raise HTTPException(403, "任务不在授权范围内")


def authorize_resource(identity, resource_id):
    """资源范围由服务端校验，创建或改绑任务不得绕过当前账号范围。"""
    allowed = identity.get("resourceIds")
    if allowed is not None and resource_id not in allowed:
        raise HTTPException(403, "资源不在授权范围内")
