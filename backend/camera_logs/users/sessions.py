"""可撤销的服务端登录会话；每次授权读取当前账号与资源范围。"""

import hashlib
from urllib.parse import urlsplit

from fastapi import HTTPException

from camera_logs.common.database import now
from camera_logs.users.passwords import hash_password, password_work

COOKIE = "camera_session"
PUBLIC_FIELDS = ("id", "username", "displayName", "isAdmin", "builtin", "scopes", "resourceIds",
                 "enabled", "mustChangePassword", "version", "createdAt", "updatedAt", "deletedAt")


def public_user(user):
    """账号只能通过白名单序列化，哈希和会话版本永不返回客户端。"""
    return {key: user.get(key) for key in PUBLIC_FIELDS}


def required_login():
    """身份错误带稳定响应头，避免前端将设备认证的 401 当成平台退出。"""
    return HTTPException(401, "登录会话已失效，请重新登录", headers={"X-Auth-Required": "true"})


def check_origin(request, *, write=False):
    """SameSite 外再验证来源和自定义请求头，拒绝跨站登录及 cookie 写操作。"""
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        expected_scheme = "https" if request.url.scheme in {"https", "wss"} else "http"
        if parsed.scheme != expected_scheme or parsed.netloc != request.headers.get("host"):
            raise HTTPException(403, "请求来源不匹配")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不允许跨站访问")
    if write and request.headers.get("x-requested-with") != "XMLHttpRequest":
        raise HTTPException(403, "缺少浏览器请求校验头")


async def initialize_admin(repo):
    """首次按部署配置创建管理员；重复启动从不覆盖已修改的密码。"""
    password = repo.settings.admin_password
    if not password or await repo.db.users.find_one({"id": "builtin-admin"}):
        return
    if len(password) < 8 or len(password) > 128:
        raise RuntimeError("ADMIN_PASSWORD 长度必须为 8 至 128 字符")
    import re
    username = repo.settings.admin_username.lower()
    if re.fullmatch(r"[a-z0-9_.-]{3,64}", username) is None:
        raise RuntimeError("ADMIN_USERNAME 格式无效")
    document = {"id": "builtin-admin", "username": username, "displayName": "系统管理员",
                "passwordHash": await password_work(repo, hash_password, password),
                "isAdmin": True, "builtin": True, "scopes": ["*"], "resourceIds": None,
                "enabled": True, "mustChangePassword": True, "version": 1, "authVersion": 1,
                "createdAt": now(), "updatedAt": now()}
    await repo.db.users.update_one({"id": "builtin-admin"}, {"$setOnInsert": document}, upsert=True)


async def user_identity(repo, user):
    """资源授权投影到当前任务 ID，复用所有现有逐任务授权入口。"""
    task_ids = None
    if user.get("resourceIds") is not None:
        task_ids = await repo.db.tasks.distinct("id", {
            "resourceId": {"$in": user["resourceIds"]}, "$or": [
                {"serialServerResourceId": None},
                {"serialServerResourceId": {"$in": user["resourceIds"]}},
            ]})
    return {**public_user(user), "kind": "session", "taskIds": task_ids,
            "scopes": ["*"] if user["isAdmin"] else user["scopes"]}


async def session_identity(repo, token):
    """会话撤销、账号禁用、重置密码和权限变更均即时生效，无权限缓存。"""
    if not token:
        raise required_login()
    session = await repo.db.user_sessions.find_one({"tokenHash": hashlib.sha256(token.encode()).hexdigest(),
                                                   "expiresAt": {"$gt": now()}})
    if not session:
        raise required_login()
    user = await repo.db.users.find_one({"id": session["userId"], "enabled": True, "deletedAt": None,
                                         "authVersion": session["authVersion"]})
    if not user:
        raise required_login()
    return await user_identity(repo, user)


def set_session_cookie(repo, token, request, response):
    """仅在数据库事务确认后将凭证放入 HttpOnly Cookie，不在此函数写数据库。"""
    seconds = repo.settings.session_seconds
    response.set_cookie(COOKIE, token, max_age=seconds, httponly=True, samesite="strict",
                        secure=repo.settings.session_cookie_secure or request.url.scheme == "https", path="/api/v1")
    response.headers["Cache-Control"] = "no-store"
