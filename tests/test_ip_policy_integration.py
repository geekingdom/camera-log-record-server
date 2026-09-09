"""验证来源 IP 策略在真实主应用的 HTTP、Cookie、Bearer 和 WebSocket 链路生效。"""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import now
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient
from starlette.websockets import WebSocketDisconnect

CSRF = {"X-Requested-With": "XMLHttpRequest"}
SOURCE_V4 = "198.51.100.9"
SOURCE_V6 = "2001:db8:1::9"
POLICY_SCOPES = ["admin", "tasks:read", "resources:create", "resources:write", "tasks:create", "tasks:control"]


@pytest.fixture
def app(tmp_path):
    """复用同一内存数据库，使不同 TestClient 来源地址观察同一策略状态。"""
    settings = Settings(_env_file=None, bootstrap_token="ip-integration-bootstrap",
                        encryption_key=Fernet.generate_key().decode(), admin_password="ip-integration-password",
                        log_root=tmp_path, start_background=False)
    return create_app(settings, AsyncMongoMockClient().camera_logs)


def policy_body(version=1, scopes=POLICY_SCOPES):
    """生成同时允许 IPv4 与 IPv6 测试客户端的候选策略。"""
    return {"version": version, "enabled": True, "rules": [
        {"label": "ipv4 office", "network": "198.51.100.0/24", "scopes": scopes},
        {"label": "ipv6 office", "network": "2001:db8:1::/48", "scopes": scopes},
    ]}


def configure_policy(client, body=None):
    """通过真实管理员路由启用策略，覆盖候选自锁与 CAS 保护链路。"""
    result = client.patch("/api/v1/admin/ip-policy", json=body or policy_body(),
                          headers={"Authorization": "Bearer ip-integration-bootstrap"})
    assert result.status_code == 200, result.text
    return result.json()


def create_user(client, username, scopes):
    """使用受策略限制的 bootstrap 管理员创建普通用户。"""
    result = client.post("/api/v1/users", json={
        "username": username, "displayName": username, "password": "initial-user-password", "scopes": scopes,
    }, headers={"Authorization": "Bearer ip-integration-bootstrap"})
    assert result.status_code == 201, result.text
    return result.json()


def login_and_change_password(client, username):
    """获得无强制改密标记的真实浏览器会话。"""
    result = client.post("/api/v1/auth/login", json={"username": username, "password": "initial-user-password"}, headers=CSRF)
    assert result.status_code == 200, result.text
    result = client.post("/api/v1/auth/password", json={
        "currentPassword": "initial-user-password", "newPassword": "changed-user-password",
    }, headers=CSRF)
    assert result.status_code == 200, result.text


def seed_network_resource(client, identifier="task-resource"):
    """插入已认证设备，隔离任务创建测试与真实海康网络调用。"""
    client.portal.call(client.app.state.repo.db.resources.insert_one, {
        "id": identifier, "name": identifier, "kind": "HIKVISION_NETWORK", "ip": "203.0.113.77",
        "model": "DS-2CD", "subSerialNumber": identifier, "authenticatedAt": datetime.now(UTC),
        "deletedAt": None, "version": 1,
    })


def task_body(resource_id):
    """构造使用非白名单设备目标 IP 的任务请求，来源限制不应读取该字段。"""
    return {"name": "source-separated", "description": "", "protocol": "SSH", "ip": "203.0.113.77",
            "port": 22, "username": "root", "password": "device-password", "resourceId": resource_id}


def test_ip_policy_blocks_unmatched_login_xff_and_websocket_but_accepts_ipv4_ipv6_sources(app):
    """来源检查发生在登录前，伪造 XFF 无效，未匹配实时订阅以 4403 关闭。"""
    with TestClient(app, client=(SOURCE_V4, 41000)) as allowed:
        configure_policy(allowed)
        assert allowed.get("/api/v1/admin/ip-policy", headers={"Authorization": "Bearer ip-integration-bootstrap"}).json()["clientIp"] == SOURCE_V4

    with TestClient(app, client=("203.0.113.99", 41001)) as denied:
        login = denied.post("/api/v1/auth/login", json={"username": "any", "password": "not-used"}, headers=CSRF)
        assert login.status_code == 403 and login.json()["error"]["code"] == "IP_ACCESS_DENIED"
        forged = denied.get("/api/v1/tasks", headers={
            "Authorization": "Bearer ip-integration-bootstrap", "X-Forwarded-For": SOURCE_V4,
        })
        assert forged.status_code == 403
        with pytest.raises(WebSocketDisconnect) as closed, denied.websocket_connect("/api/v1/tasks/missing/logs") as socket:
            socket.send_json({"token": "ip-integration-bootstrap"})
            socket.receive_json()
        assert closed.value.code == 4403

    with TestClient(app, client=(SOURCE_V6, 41002)) as ipv6:
        response = ipv6.get("/api/v1/admin/ip-policy", headers={"Authorization": "Bearer ip-integration-bootstrap"})
        assert response.status_code == 200 and response.json()["clientIp"] == SOURCE_V6


def test_bearer_and_cookie_scopes_are_intersected_and_policy_changes_block_existing_cookie(app):
    """相同来源规则仅保留交集；策略更新后旧 cookie 不得保留原有读权限。"""
    with TestClient(app, client=(SOURCE_V4, 42000)) as client:
        configure_policy(client)
        repo = client.app.state.repo
        client.portal.call(repo.db.users.insert_one, {
            "id": "limited-bearer-user", "username": "limited-bearer", "displayName": "受限令牌用户",
            "isAdmin": False, "scopes": ["tasks:create"], "enabled": True, "deletedAt": None,
        })
        client.portal.call(repo.db.tokens.insert_one, {
            "id": "limited-bearer", "userId": "limited-bearer-user", "version": 1,
            "tokenHash": hashlib.sha256(b"limited-bearer-token").hexdigest(), "revoked": False,
            "expiresAt": now() + timedelta(hours=1),
        })
        assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer limited-bearer-token"}).status_code == 200
        reader = create_user(client, "reader", ["tasks:read", "tasks:create"])
        login_and_change_password(client, "reader")
        assert client.get("/api/v1/tasks").status_code == 200

        changed = configure_policy(client, policy_body(version=2, scopes=["admin"]))
        assert changed["version"] == 3
        assert client.get("/api/v1/tasks").status_code == 403
        assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer limited-bearer-token"}).status_code == 403
        assert reader["username"] == "reader"


def test_service_token_reading_is_denied_when_source_policy_omits_its_scope(app):
    """基础的本人令牌读取权限也必须与来源 IP 策略取交集。"""
    with TestClient(app, client=(SOURCE_V4, 42500)) as client:
        configure_policy(client)
        user = create_user(client, "policy-token-reader", [])
        created = client.post("/api/v1/service-tokens", json={
            "name": "来源限制", "userId": user["id"], "expiresInDays": 1,
        }, headers={"Authorization": "Bearer ip-integration-bootstrap"})
        assert created.status_code == 201, created.text
        headers = {"Authorization": "Bearer " + created.json()["token"]}
        assert client.get("/api/v1/service-tokens", headers=headers).status_code == 403
        assert client.post(f"/api/v1/service-tokens/{created.json()['id']}/reveal", headers=headers).status_code == 403


def test_resource_and_task_creation_scopes_are_independent_and_ignore_device_target_ip(app, monkeypatch):
    """访问来源可创建设备目标不在 CIDR 内的资源，且 resources:create 与 tasks:create 不能互通。"""
    async def verified(**_kwargs):
        return {"model": "DS-2CD", "subSerialNumber": "TARGET-OUTSIDE", "softwareVersion": "V5"}

    monkeypatch.setattr("camera_logs.resources.api.authenticate_network_resource", verified)
    with TestClient(app, client=(SOURCE_V4, 43000)) as client:
        configure_policy(client)
        resource_user = create_user(client, "resource-only", ["resources:create"])
        login_and_change_password(client, "resource-only")
        resource = client.post("/api/v1/resources", json={
            "name": "outside device", "kind": "HIKVISION_NETWORK", "ip": "203.0.113.77",
            "username": "admin", "password": "device-password", "authType": "DIGEST",
        }, headers=CSRF | {"Idempotency-Key": uuid4().hex})
        assert resource.status_code == 201, resource.text
        assert client.post("/api/v1/tasks", json=task_body(resource.json()["id"]),
                           headers=CSRF | {"Idempotency-Key": uuid4().hex}).status_code == 403
        assert client.post("/api/v1/auth/logout", headers=CSRF).status_code == 204

        task_user = create_user(client, "task-only", ["tasks:create", "tasks:control"])
        seed_network_resource(client)
        login_and_change_password(client, "task-only")
        assert client.post("/api/v1/resources", json={
            "name": "forbidden resource", "kind": "HIKVISION_NETWORK", "ip": "203.0.113.77",
            "username": "admin", "password": "device-password", "authType": "DIGEST",
        }, headers=CSRF | {"Idempotency-Key": uuid4().hex}).status_code == 403
        task = client.post("/api/v1/tasks", json=task_body("task-resource"),
                           headers=CSRF | {"Idempotency-Key": uuid4().hex})
        assert task.status_code == 201, task.text
        client.portal.call(client.app.state.repo.db.resources.insert_one, {
            "id": "serial-target-outside", "name": "outside serial", "kind": "SERIAL_SERVER",
            "ip": "203.0.113.88", "deletedAt": None, "version": 1,
        })
        serial_task = client.post("/api/v1/tasks", json={
            "name": "serial target outside", "description": "", "protocol": "TELNET_SERIAL",
            "ip": "203.0.113.88", "port": 10003, "resourceId": "task-resource",
            "serialServerResourceId": "serial-target-outside",
        }, headers=CSRF | {"Idempotency-Key": uuid4().hex})
        assert serial_task.status_code == 201, serial_task.text
        started = client.post(f"/api/v1/tasks/{serial_task.json()['id']}/start", headers=CSRF)
        assert started.status_code == 202, started.text
        assert resource_user["username"] == "resource-only" and task_user["username"] == "task-only"


def test_real_route_rejects_self_lock_and_stale_policy_version(app):
    """候选规则未覆盖当前管理员来源或缺少 admin 时拒绝，CAS 不覆盖新版本。"""
    with TestClient(app, client=(SOURCE_V4, 44000)) as client:
        headers = {"Authorization": "Bearer ip-integration-bootstrap"}
        self_locked = client.patch("/api/v1/admin/ip-policy", json={
            "version": 1, "enabled": True,
            "rules": [{"label": "other", "network": "203.0.113.0/24", "scopes": ["admin"]}],
        }, headers=headers)
        assert self_locked.status_code == 422
        saved = configure_policy(client)
        assert saved["version"] == 2
        stale = client.patch("/api/v1/admin/ip-policy", json={"version": 1, "enabled": False, "rules": []}, headers=headers)
        assert stale.status_code == 409
