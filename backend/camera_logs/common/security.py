"""处理 Bearer Token 身份识别、作用域校验和任务范围限制。"""

import hashlib
import secrets

from fastapi import HTTPException, Request

from camera_logs.access_policy.policy import apply_ip_permissions
from camera_logs.common.database import now
from camera_logs.common.request_context import request_context
from camera_logs.users.sessions import COOKIE, check_origin, required_login, session_identity, user_identity


async def authenticate(repo, token):
    """认证系统令牌或绑定用户的服务令牌，实时读取用户权限和启用状态。"""
    if token and repo.settings.bootstrap_token and secrets.compare_digest(token, repo.settings.bootstrap_token):
        return {"id": "bootstrap", "scopes": ["*"], "isAdmin": True,
                "resourceIds": None, "taskIds": None}
    digest = hashlib.sha256(token.encode()).hexdigest()
    record = await repo.db.tokens.find_one({"tokenHash": digest, "revoked": False})
    expires_at = record.get("expiresAt") if record else None
    if not record or record.get("revoked") or (expires_at is not None and expires_at.replace(tzinfo=now().tzinfo) <= now()):
        raise required_login()
    user = await repo.db.users.find_one({"id": record.get("userId"), "enabled": True, "deletedAt": None})
    if user is None:
        raise required_login()
    return await user_identity(repo, user) | {"kind": "service-token", "serviceTokenId": record["id"]}


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
    context = request_context.get()
    if context is not None and identity.get("serviceTokenId"):
        context["serviceTokenId"] = identity["serviceTokenId"]
    request.state.actor = identity
    return identity


def authorize(identity, scope, task_id=None):
    """验证实时用户作用域；任务可见性不再由用户或令牌范围收窄。"""
    if "*" not in identity["scopes"] and scope not in identity["scopes"]:
        raise HTTPException(403, "权限不足")


def authorize_resource(identity, resource_id):
    """保留资源鉴权调用兼容性；资源范围已不再作为身份限制。"""


def authorize_owner(identity, document):
    """管理员可操作全部对象，普通用户只能操作自己创建的对象。"""
    if identity.get("isAdmin") or "*" in identity.get("scopes", []):
        return
    if document.get("createdBy") != identity.get("id"):
        raise HTTPException(403, "仅创建者可以执行此操作")
