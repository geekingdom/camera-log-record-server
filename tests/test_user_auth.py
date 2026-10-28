"""验证内置管理员、浏览器会话和子账户管理的安全边界。"""

import json
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


def test_login_failure_audit_keeps_submitted_username_without_credentials(client, monkeypatch):
    """登录失败及限流仍是匿名主体，只保留用户实际填写的账号。"""
    async def failed_password_work(*_args, **_kwargs):
        return False

    monkeypatch.setattr("camera_logs.users.api.password_work", failed_password_work)
    submitted_username = "Audit.User "
    for _ in range(10):
        response = login(client, submitted_username, "never-store-this-password")
        assert response.status_code == 401, response.text
    limited = login(client, submitted_username, "never-store-this-password")
    assert limited.status_code == 429, limited.text

    async def failure_audits():
        return [row async for row in client.app.state.repo.db.audit.find({"action": "login_failed"})]

    audits = client.portal.call(failure_audits)
    assert len(audits) == 11
    for audit in audits:
        assert audit["actor"] == "anonymous"
        assert audit["metadata"] == {"username": submitted_username}
        serialized = json.dumps(audit, default=str)
        assert "password" not in serialized and "passwordHash" not in serialized


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


def test_deleted_username_can_be_recreated_with_new_identity_without_reviving_old_session(client):
    """软删除账户释放用户名，新账户不能继承旧会话或身份关联。"""
    change_initial_password(client)
    original = create_operator(client, username="reusable-name")
    assert client.post("/api/v1/auth/logout", headers=CSRF).status_code == 204
    assert login(client, "reusable-name", "operator-password").status_code == 200
    old_cookie = client.cookies.get("camera_session")

    client.headers["Authorization"] = "Bearer test-bootstrap"
    token_response = client.post("/api/v1/service-tokens", json={
        "name": "旧用户令牌", "userId": original["id"],
    })
    assert token_response.status_code == 201, token_response.text
    old_service_token = token_response.json()["token"]
    assert client.delete(f"/api/v1/users/{original['id']}?version={original['version']}").status_code == 204
    recreated = client.post("/api/v1/users", json={
        "username": "REUSABLE-NAME", "displayName": "新操作员", "password": "replacement-password",
    })
    del client.headers["Authorization"]

    assert recreated.status_code == 201, recreated.text
    replacement = recreated.json()
    assert replacement["id"] != original["id"]
    assert replacement["username"] == "reusable-name"
    client.cookies.set("camera_session", old_cookie, path="/api/v1")
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/users/share-targets", headers={
        "Authorization": "Bearer " + old_service_token,
    }).status_code == 401
    client.cookies.delete("camera_session", path="/api/v1")
    assert login(client, "reusable-name", "replacement-password").status_code == 200

    client.headers["Authorization"] = "Bearer test-bootstrap"
    duplicate = client.post("/api/v1/users", json={
        "username": "reusable-name", "displayName": "仍应冲突", "password": "another-password",
    })
    del client.headers["Authorization"]
    assert duplicate.status_code == 409


def test_delete_user_requires_resource_disposition_and_reuses_resource_lifecycle(client):
    """有活动资源的账户必须显式处置，DELETE 走资源软删除及任务停止语义。"""
    change_initial_password(client)
    owner = create_operator(client, username="resource-owner")
    repo = client.app.state.repo
    client.portal.call(repo.db.resources.insert_one, {
        "id": "owner-resource", "name": "归属资源", "kind": "SERIAL_SERVER", "ip": "192.0.2.210",
        "version": 1, "createdBy": owner["id"], "createdByName": owner["displayName"],
    })
    client.portal.call(repo.db.resource_addresses.insert_one, {"_id": "192.0.2.210", "resourceId": "owner-resource"})
    client.portal.call(repo.db.tasks.insert_one, {
        "id": "owner-running-task", "resourceId": "owner-resource", "createdBy": owner["id"],
        "desiredState": "RUNNING", "status": "PENDING",
    })

    missing = client.delete(f"/api/v1/users/{owner['id']}?version={owner['version']}", headers=CSRF)
    assert missing.status_code == 422
    assert client.portal.call(repo.db.users.find_one, {"id": owner["id"]}).get("deletedAt") is None

    deleted = client.delete(
        f"/api/v1/users/{owner['id']}?version={owner['version']}&resourceAction=DELETE", headers=CSRF,
    )
    assert deleted.status_code == 204, deleted.text
    resource = client.portal.call(repo.db.resources.find_one, {"id": "owner-resource"})
    task = client.portal.call(repo.db.tasks.find_one, {"id": "owner-running-task"})
    assert resource["deletedAt"] is not None and resource["deletionState"] == "PENDING"
    assert task["deletedAt"] is not None and task["desiredState"] == "STOPPED"
    assert client.portal.call(repo.db.resource_addresses.find_one, {"_id": "192.0.2.210"}) is None
    assert client.portal.call(repo.db.audit.count_documents, {"action": "delete_user", "targetId": owner["id"]}) == 1


def test_delete_user_transfer_moves_only_owned_associated_tasks(client, monkeypatch):
    """转移资源时保留审计历史，其他用户创建的关联任务不得被夺取或停止。"""
    change_initial_password(client)
    owner = create_operator(client, username="transfer-owner")
    target = create_operator(client, username="transfer-target")
    other = create_operator(client, username="transfer-other")
    repo = client.app.state.repo
    transfer_time = now()
    monkeypatch.setattr("camera_logs.users.api.now", lambda: transfer_time)
    client.portal.call(repo.db.resources.insert_one, {
        "id": "transfer-resource", "name": "待转移资源", "kind": "SERIAL_SERVER", "ip": "192.0.2.211",
        "version": 3, "createdBy": owner["id"], "createdByName": owner["displayName"],
    })
    client.portal.call(repo.db.tasks.insert_many, [
        {"id": "move-task", "resourceId": "transfer-resource", "createdBy": owner["id"],
         "createdByName": owner["displayName"], "version": 2, "desiredState": "RUNNING", "status": "COLLECTING"},
        {"id": "keep-task", "resourceId": "transfer-resource", "createdBy": other["id"],
         "createdByName": other["displayName"], "version": 2, "desiredState": "RUNNING", "status": "COLLECTING"},
        {"id": "target-existing-task", "resourceId": "transfer-resource", "createdBy": target["id"],
         "createdByName": target["displayName"], "version": 2, "updatedAt": transfer_time,
         "desiredState": "RUNNING", "status": "COLLECTING"},
    ])
    result = client.delete(
        f"/api/v1/users/{owner['id']}?version={owner['version']}&resourceAction=TRANSFER&targetUserId={target['id']}",
        headers=CSRF,
    )
    assert result.status_code == 204, result.text
    resource = client.portal.call(repo.db.resources.find_one, {"id": "transfer-resource"})
    moved = client.portal.call(repo.db.tasks.find_one, {"id": "move-task"})
    kept = client.portal.call(repo.db.tasks.find_one, {"id": "keep-task"})
    assert resource["createdBy"] == target["id"] and resource["createdByName"] == target["displayName"]
    assert resource["version"] == 4 and resource.get("deletedAt") is None
    assert moved["createdBy"] == target["id"] and moved["version"] == 3
    assert kept["createdBy"] == other["id"] and kept["desiredState"] == "RUNNING"
    async def transfer_audit_ids():
        return [item["targetId"] async for item in repo.db.audit.find({"action": "transfer_user_task"})]

    audited_task_ids = client.portal.call(transfer_audit_ids)
    assert audited_task_ids == ["move-task"]


def test_delete_user_transfer_rejects_invalid_target(client):
    """转移不能指向自己、停用或已删除账号，避免资源落入无效归属。"""
    change_initial_password(client)
    owner = create_operator(client, username="invalid-target-owner")
    repo = client.app.state.repo
    client.portal.call(repo.db.resources.insert_one, {
        "id": "invalid-target-resource", "kind": "SERIAL_SERVER", "ip": "192.0.2.212", "version": 1,
        "createdBy": owner["id"], "createdByName": owner["displayName"],
    })
    response = client.delete(
        f"/api/v1/users/{owner['id']}?version={owner['version']}&resourceAction=TRANSFER&targetUserId={owner['id']}",
        headers=CSRF,
    )
    assert response.status_code == 422


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


def test_current_identity_reports_only_active_service_tokens(client):
    """文档入口只承认当前用户未撤销且未到期的服务账号。"""
    change_initial_password(client)
    operator = create_operator(client, username="api-reference-user")
    assert client.post("/api/v1/auth/logout", headers=CSRF).status_code == 204
    assert login(client, "api-reference-user", "operator-password").json()["user"]["hasActiveServiceToken"] is False

    client.headers["Authorization"] = "Bearer test-bootstrap"
    created = client.post("/api/v1/service-tokens", json={"name": "文档账号", "userId": operator["id"]})
    assert created.status_code == 201, created.text
    del client.headers["Authorization"]
    assert client.get("/api/v1/auth/me").json()["user"]["hasActiveServiceToken"] is True

    client.headers["Authorization"] = "Bearer test-bootstrap"
    assert client.delete(f"/api/v1/service-tokens/{created.json()['id']}").status_code == 204
    expired = client.post("/api/v1/service-tokens", json={"name": "过期账号", "userId": operator["id"]})
    client.portal.call(client.app.state.repo.db.tokens.update_one, {"id": expired.json()["id"]},
                       {"$set": {"expiresAt": now() - timedelta(seconds=1)}})
    del client.headers["Authorization"]
    assert client.get("/api/v1/auth/me").json()["user"]["hasActiveServiceToken"] is False


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
