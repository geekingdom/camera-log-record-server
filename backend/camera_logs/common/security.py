"""处理 Bearer Token 身份识别、作用域校验和任务范围限制。"""

import hashlib
import secrets

from fastapi import HTTPException, Request

from camera_logs.common.database import now


async def authenticate(repo, token):
    """校验 bootstrap 或持久化令牌，过期和撤销令牌统一返回 401。"""
    if token and repo.settings.bootstrap_token and secrets.compare_digest(token, repo.settings.bootstrap_token):
        return {"id": "bootstrap", "scopes": ["*"], "taskIds": None}
    digest = hashlib.sha256(token.encode()).hexdigest()
    record = await repo.db.tokens.find_one({"tokenHash": digest, "revoked": False})
    if not record or record["expiresAt"].replace(tzinfo=now().tzinfo) <= now():
        raise HTTPException(401, "访问令牌无效或已过期")
    return record


async def actor(request: Request):
    """从 Authorization 提取身份并写入 request.state，供审计和访问日志复用。"""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "需要 Bearer Token")
    identity = await authenticate(request.app.state.repo, auth[7:])
    request.state.actor = identity
    return identity


def authorize(identity, scope, task_id=None):
    """同时验证作用域和可选任务白名单，权限不足统一返回 403。"""
    if "*" not in identity["scopes"] and scope not in identity["scopes"]:
        raise HTTPException(403, "权限不足")
    allowed = identity.get("taskIds")
    if task_id is not None and allowed is not None and task_id not in allowed:
        raise HTTPException(403, "任务不在授权范围内")
