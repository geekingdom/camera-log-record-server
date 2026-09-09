"""浏览器原生下载的短期授权，避免把大文件读入前端 Blob。

登录令牌换取仅对单个下载 URL 生效的 HttpOnly Cookie，数据库只保存摘要。
下载时再次验证账号有效期及撤销状态；第三方仍可直接使用 Bearer Token。
"""
import hashlib
import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from pymongo.errors import PyMongoError

from camera_logs.access_policy.policy import apply_ip_permissions
from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.request_context import request_context
from camera_logs.common.security import actor, authorize
from camera_logs.users.sessions import COOKIE, user_identity


async def issue_download_ticket(repo, identity, identifier):
    """固定票据与审计原子提交；确认丢失只读恢复，确认前不得发送 Cookie。"""
    token = secrets.token_urlsafe(32)
    document = {"tokenHash": hashlib.sha256(token.encode()).hexdigest(),
                "jobId": identifier, "actor": identity["id"], "serviceTokenId": identity.get("serviceTokenId"),
                "expiresAt": now() + timedelta(minutes=5)}

    async def commit(session):
        await repo.db.download_sessions.insert_one(document, session=session)
        await repo.audit(identity["id"], "browser_download_authorization", identifier, session=session)

    try:
        await audited_mutations.mutation_transaction(repo, commit)
    except PyMongoError as error:
        try:
            database = audited_mutations._majority_primary_database(repo)
            confirmed = await database.download_sessions.find_one({
                "tokenHash": document["tokenHash"], "jobId": identifier,
                "actor": identity["id"], "expiresAt": {"$gt": now()},
            })
        except PyMongoError:
            confirmed = None
        if confirmed is None:
            raise HTTPException(503, "下载授权提交结果未知，请重新申请下载授权") from error
    return token


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
        user = await repo.db.users.find_one({"id": document["actor"], "enabled": True, "deletedAt": None})
        if user is None:
            raise HTTPException(401, "访问令牌已失效")
        if document.get("serviceTokenId") and not await repo.db.tokens.find_one({
            "id": document["serviceTokenId"], "revoked": False, "userId": user["id"],
            "$or": [{"expiresAt": None}, {"expiresAt": {"$gt": now()}}],
        }):
            raise HTTPException(401, "访问令牌已失效")
        identity = await user_identity(repo, user)
        if document.get("serviceTokenId"):
            identity |= {"kind": "service-token", "serviceTokenId": document["serviceTokenId"]}
    else:
        identity = {"id": "bootstrap", "scopes": ["*"], "taskIds": None}
    # 无平台会话的第三方下载票据也必须受当前客户端 IP 权限限制。
    identity = await apply_ip_permissions(repo, request, identity)
    context = request_context.get()
    if context is not None and identity.get("serviceTokenId"):
        context["serviceTokenId"] = identity["serviceTokenId"]
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
        token = await issue_download_ticket(repo, user, identifier)
        path = f"/api/v1/downloads/{identifier}/content"
        response.set_cookie("download_access", token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                            path=path, max_age=300)
        return {"url": path, "expiresInSeconds": 300}
