"""验证内置管理员、浏览器会话和子账户管理的安全边界。"""

from datetime import timedelta

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import now
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

CSRF = {"X-Requested-With": "XMLHttpRequest"}
ADMIN_PASSWORD = "initial-admin-password"


@pytest.fixture
def client(tmp_path):
    """启动使用显式测试配置的 API，绝不读取本机 .env 中的管理员密码。"""
    settings = Settings(
        _env_file=None,
        bootstrap_token="test-bootstrap",
        encryption_key=Fernet.generate_key().decode(),
        admin_username="Root.Admin",
        admin_password=ADMIN_PASSWORD,
        log_root=tmp_path,
        start_background=False,
    )
    with TestClient(create_app(settings, AsyncMongoMockClient().camera_logs)) as result:
        yield result


def login(client, username="root.admin", password=ADMIN_PASSWORD):
    """以浏览器请求头登录，复用真实 cookie 会话路径。"""
    return client.post("/api/v1/auth/login", json={"username": username, "password": password}, headers=CSRF)


def change_initial_password(client, password="changed-admin-password"):
    """完成内置管理员首登强制改密，以便调用后续管理 API。"""
    logged_in = login(client)
    assert logged_in.status_code == 200, logged_in.text
    result = client.post("/api/v1/auth/password", json={
        "currentPassword": ADMIN_PASSWORD, "newPassword": password,
    }, headers=CSRF)
    assert result.status_code == 200, result.text
    return password


def create_operator(client, *, username="operator", scopes=None):
    """创建一个可登录的普通账号，并返回公开用户视图。"""
    result = client.post("/api/v1/users", json={
        "username": username,
        "displayName": "操作员",
        "password": "operator-password",
        "scopes": scopes or [],
    }, headers=CSRF)
    assert result.status_code == 201, result.text
    return result.json()


def test_builtin_admin_initializes_requires_password_change_and_never_leaks_hash(client):
    """部署配置首次初始化账号，首登仅允许 me、改密和退出。"""
    me = login(client)
    assert me.status_code == 200, me.text
    user = me.json()["user"]
    assert user["username"] == "root.admin"
    assert user["builtin"] is True
    assert user["mustChangePassword"] is True
    assert "camera_session" in me.headers["set-cookie"] and "HttpOnly" in me.headers["set-cookie"]
    assert "passwordHash" not in me.text and "authVersion" not in me.text

    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.get("/api/v1/users").status_code == 403
    assert client.post("/api/v1/auth/password", json={
        "currentPassword": ADMIN_PASSWORD, "newPassword": "short",
    }, headers=CSRF).status_code == 422
    change_initial_password(client)
    listing = client.get("/api/v1/users")
    assert listing.status_code == 200
    assert "passwordHash" not in listing.text and "authVersion" not in listing.text


def test_cookie_logout_expiry_disable_and_permission_change_invalidate_sessions(client):
    """会话在退出、过期、禁用和账户版本变更后都必须立即失效。"""
    change_initial_password(client)
    operator = create_operator(client)
    password = "operator-password"

    assert client.post("/api/v1/auth/logout", headers=CSRF).status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401
    assert login(client, "operator", password).status_code == 200
    token = client.cookies.get("camera_session")
    repo = client.app.state.repo
    client.portal.call(repo.db.user_sessions.update_one, {"tokenHash": __import__("hashlib").sha256(token.encode()).hexdigest()},
                       {"$set": {"expiresAt": now() - timedelta(seconds=1)}})
    assert client.get("/api/v1/auth/me").status_code == 401

    assert login(client, "operator", password).status_code == 200
    assert client.patch(f"/api/v1/users/{operator['id']}", json={"version": 1, "enabled": False}, headers=CSRF).status_code == 403
    client.headers["Authorization"] = "Bearer test-bootstrap"
    assert client.patch(f"/api/v1/users/{operator['id']}", json={"version": 1, "enabled": False}).status_code == 200
    del client.headers["Authorization"]
    assert client.get("/api/v1/auth/me").status_code == 401

    client.headers["Authorization"] = "Bearer test-bootstrap"
    client.portal.call(repo.db.users.update_one, {"id": operator["id"]}, {"$set": {"enabled": True}, "$inc": {"version": 1, "authVersion": 1}})
    del client.headers["Authorization"]
    assert login(client, "operator", password).status_code == 200
    client.headers["Authorization"] = "Bearer test-bootstrap"
    version = client.portal.call(repo.db.users.find_one, {"id": operator["id"]})["version"]
    assert client.patch(f"/api/v1/users/{operator['id']}", json={"version": version, "scopes": ["tasks:read", "logs:read"]}).status_code == 200
    del client.headers["Authorization"]
    assert client.get("/api/v1/auth/me").status_code == 401


def test_csrf_duplicate_username_builtin_protection_and_password_policy(client):
    """管理写操作与登录 cookie 写操作均需 CSRF 头，内置账号不能被管理接口修改。"""
    assert client.post("/api/v1/auth/login", json={"username": "root.admin", "password": ADMIN_PASSWORD}).status_code == 403
    change_initial_password(client)
    created = create_operator(client)
    assert client.post("/api/v1/users", json={
        "username": "OPERATOR", "displayName": "重复", "password": "operator-password",
    }, headers=CSRF).status_code == 409
    assert client.post("/api/v1/users", json={
        "username": "tiny", "displayName": "短密码", "password": "seven77",
    }, headers=CSRF).status_code == 422
    builtin = client.get("/api/v1/users").json()["items"]
    builtin_id = next(item["id"] for item in builtin if item["builtin"])
    assert client.patch(f"/api/v1/users/{builtin_id}", json={"version": 1, "enabled": False}, headers=CSRF).status_code == 403
    assert client.post(f"/api/v1/users/{builtin_id}/reset-password", json={
        "version": 1, "password": "replacement-password",
    }, headers=CSRF).status_code == 403
    assert client.delete(f"/api/v1/users/{builtin_id}?version=1", headers=CSRF).status_code == 403
    assert "password" not in created and "passwordHash" not in created and "authVersion" not in created


def test_user_password_policy_accepts_eight_characters_for_create_reset_and_change(client):
    """平台用户创建、管理员重置与本人改密共享 8 至 128 位下限。"""
    assert client.post("/api/v1/auth/login", json={"username": "root.admin", "password": ADMIN_PASSWORD}).status_code == 403
    current = change_initial_password(client, "initial8")
    assert client.post("/api/v1/users", json={
        "username": "seven", "displayName": "七位密码", "password": "seven77",
    }, headers=CSRF).status_code == 422
    created = client.post("/api/v1/users", json={
        "username": "eight", "displayName": "八位密码", "password": "eight888",
    }, headers=CSRF)
    assert created.status_code == 201, created.text
    assert client.post(f"/api/v1/users/{created.json()['id']}/reset-password", json={
        "version": created.json()["version"], "password": "seven77",
    }, headers=CSRF).status_code == 422
    assert client.post(f"/api/v1/users/{created.json()['id']}/reset-password", json={
        "version": created.json()["version"], "password": "reset888",
    }, headers=CSRF).status_code == 200
    assert client.post("/api/v1/auth/password", json={
        "currentPassword": current, "newPassword": "seven77",
    }, headers=CSRF).status_code == 422
    changed = client.post("/api/v1/auth/password", json={
        "currentPassword": current, "newPassword": "changed8",
    }, headers=CSRF)
    assert changed.status_code == 200, changed.text


def test_login_and_password_change_return_current_effective_base_scopes(client):
    """首次登录和改密响应须与后续 me 一致，直接呈现基础模板和日志权限。"""
    change_initial_password(client)
    create_operator(client, username="effective-scopes")
    assert client.post("/api/v1/auth/logout", headers=CSRF).status_code == 204
    logged_in = login(client, "effective-scopes", "operator-password")
    assert logged_in.status_code == 200, logged_in.text
    expected = {"tasks:read", "logs:read", "logs:download", "templates:read", "templates:write"}
    assert expected <= set(logged_in.json()["user"]["scopes"])
    changed = client.post("/api/v1/auth/password", json={
        "currentPassword": "operator-password", "newPassword": "effective-scopes-password",
    }, headers=CSRF)
    assert changed.status_code == 200, changed.text
    assert set(changed.json()["user"]["scopes"]) == set(client.get("/api/v1/auth/me").json()["user"]["scopes"])


def test_share_targets_returns_only_active_users_for_cookie_and_service_token(client):
    """模板共享目录允许有效调用者分页查询，且不泄露账号安全字段。"""
    change_initial_password(client)
    reader = create_operator(client, username="share-reader")
    visible = create_operator(client, username="share-visible")
    disabled = create_operator(client, username="share-disabled")
    deleted = create_operator(client, username="share-deleted")
    repo = client.app.state.repo
    client.portal.call(repo.db.users.update_one, {"id": disabled["id"]}, {"$set": {"enabled": False}})
    client.portal.call(repo.db.users.update_one, {"id": deleted["id"]}, {"$set": {"deletedAt": now()}})

    client.headers["Authorization"] = "Bearer test-bootstrap"
    token_response = client.post("/api/v1/service-tokens", json={"name": "共享目录", "userId": reader["id"]})
    assert token_response.status_code == 201, token_response.text
    del client.headers["Authorization"]

    assert client.post("/api/v1/auth/logout", headers=CSRF).status_code == 204
    assert login(client, "share-reader", "operator-password").status_code == 200
    assert client.post("/api/v1/auth/password", json={
        "currentPassword": "operator-password", "newPassword": "share-reader-password",
    }, headers=CSRF).status_code == 200

    cookie_page = client.get("/api/v1/users/share-targets?page=1&pageSize=1")
    assert cookie_page.status_code == 200, cookie_page.text
    assert cookie_page.json()["total"] == 3
    assert len(cookie_page.json()["items"]) == 1
    assert set(cookie_page.json()["items"][0]) == {"id", "username", "displayName"}

    bearer_page = client.get("/api/v1/users/share-targets?page=2&pageSize=2", headers={
        "Authorization": "Bearer " + token_response.json()["token"],
    })
    assert bearer_page.status_code == 200, bearer_page.text
    assert bearer_page.json()["total"] == 3
    returned_ids = {item["id"] for item in bearer_page.json()["items"]}
    assert returned_ids <= {reader["id"], visible["id"]}
    assert disabled["id"] not in returned_ids and deleted["id"] not in returned_ids
