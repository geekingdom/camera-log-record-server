"""浏览器原生下载的短期授权，避免把大文件读入前端 Blob。

登录令牌换取仅对单个下载 URL 生效的 HttpOnly Cookie，数据库只保存摘要。
下载时再次验证账号有效期及撤销状态；第三方仍可直接使用 Bearer Token。
"""
import hashlib
import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response

from camera_logs.access_policy.policy import apply_ip_permissions
from camera_logs.common.database import now
from camera_logs.common.security import actor, authorize
from camera_logs.users.sessions import COOKIE


async def download_actor(request: Request):
    """校验请求头或作用于本作业的下载票据，身份权限在内容路由再次检查。"""
    if request.headers.get("authorization") or request.cookies.get(COOKIE):
        return await actor(request)
    ticket = request.cookies.get("download_access", "")
    repo = request.app.state.repo
    document = await repo.db.download_sessions.find_one({
        "tokenHash": hashlib.sha256(ticket.encode()).hexdigest(),
        "jobId": request.path_params["identifier"], "expiresAt": {"$gt": now()}})
    if document is None:
        raise HTTPException(401, "下载授权不存在或已过期")
    if document["actor"] != "bootstrap":
        identity = await repo.db.tokens.find_one({"id": document["actor"], "revoked": False, "expiresAt": {"$gt": now()}})
        if not identity:
            raise HTTPException(401, "访问令牌已失效")
    else:
        identity = {"id": "bootstrap", "scopes": ["*"], "taskIds": None}
    # 无平台会话的第三方下载票据也必须受当前客户端 IP 权限限制。
    identity = await apply_ip_permissions(repo, request, identity)
    request.state.actor = identity
    return identity


def install_download_sessions(app):
    """签发五分钟下载票据，完整路径隔离作业，SameSite 限制跨站使用。"""
    @app.post("/api/v1/downloads/{identifier}/browser-session")
    async def browser_session(identifier: str, request: Request, response: Response, user: Annotated[dict, Depends(actor)]):
        repo = request.app.state.repo
        job = await repo.get("jobs", identifier)
        authorize(user, "logs:download", job["taskId"])
        if job["status"] != "SUCCEEDED":
            raise HTTPException(409, "导出尚未完成")
        token = secrets.token_urlsafe(32)
        await repo.db.download_sessions.insert_one({"tokenHash": hashlib.sha256(token.encode()).hexdigest(),
            "jobId": identifier, "actor": user["id"], "expiresAt": now()+timedelta(minutes=5)})
        path = f"/api/v1/downloads/{identifier}/content"
        response.set_cookie("download_access", token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                            path=path, max_age=300)
        await repo.audit(user["id"], "browser_download_authorization", identifier)
        return {"url": path, "expiresInSeconds": 300}
