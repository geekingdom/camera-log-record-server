"""验证平台客户端来源白名单只收窄权限，且管理员不能通过规则把自己锁在平台外。"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from camera_logs.access_policy.api import install_ip_policy_routes
from camera_logs.access_policy.policy import POLICY_ID, apply_ip_permissions, enforce_ip
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient


@pytest.fixture
def app_repo(tmp_path):
    """构造没有主应用中间件的最小管理 API，隔离策略接口本身。"""
    repo = Repository(AsyncMongoMockClient().camera_logs, Settings(
        _env_file=None, bootstrap_token="unused", encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
    ))
    app = FastAPI()
    app.state.repo = repo
    install_ip_policy_routes(app)
    yield app, repo


def insert_admin_token(client, repo):
    """同步 TestClient 门户写入令牌记录，供真实 actor 依赖验证。"""
    import hashlib

    token = "ip-policy-admin-token"
    client.portal.call(repo.db.users.insert_one, {
        "id": "ip-policy-admin-user", "username": "ip-policy-admin", "displayName": "策略管理员",
        "isAdmin": True, "scopes": [], "enabled": True, "deletedAt": None,
    })
    client.portal.call(repo.db.tokens.insert_one, {
        "id": "ip-policy-admin", "userId": "ip-policy-admin-user", "version": 1,
        "tokenHash": hashlib.sha256(token.encode()).hexdigest(), "revoked": False, "expiresAt": now() + timedelta(hours=1),
    })
    return {"Authorization": f"Bearer {token}"}


def test_ip_policy_defaults_off_and_validates_rules_cas_and_self_lock(app_repo):
    """默认关闭，启用时拒绝空规则、无效规则、陈旧版本和管理员自锁。"""
    app, repo = app_repo
    with TestClient(app, client=("203.0.113.9", 50000)) as client:
        headers = insert_admin_token(client, repo)
        initial = client.get("/api/v1/admin/ip-policy", headers=headers)
        assert initial.status_code == 200
        assert initial.json() == {"version": 1, "enabled": False, "rules": [], "clientIp": "203.0.113.9"}
        assert client.patch("/api/v1/admin/ip-policy", headers=headers, json={
            "version": 1, "enabled": True, "rules": [],
        }).status_code == 422
        assert client.patch("/api/v1/admin/ip-policy", headers=headers, json={
            "version": 1, "enabled": True,
            "rules": [{"label": "invalid", "network": "not-a-network", "scopes": ["admin"]}],
        }).status_code == 422
        assert client.patch("/api/v1/admin/ip-policy", headers=headers, json={
            "version": 1, "enabled": True,
            "rules": [{"label": "unknown", "network": "203.0.113.0/24", "scopes": ["unknown:scope"]}],
        }).status_code == 422
        locked = client.patch("/api/v1/admin/ip-policy", headers=headers, json={
            "version": 1, "enabled": True,
            "rules": [{"label": "other office", "network": "198.51.100.0/24", "scopes": ["admin"]}],
        })
        assert locked.status_code == 422
        saved = client.patch("/api/v1/admin/ip-policy", headers=headers, json={
            "version": 1, "enabled": True,
            "rules": [{"label": "office", "network": "203.0.113.9", "scopes": ["admin", "tasks:read"]}],
        })
        assert saved.status_code == 200 and saved.json()["version"] == 2
        assert client.patch("/api/v1/admin/ip-policy", headers=headers, json={
            "version": 1, "enabled": False, "rules": [],
        }).status_code == 409


@pytest.mark.asyncio
async def test_overlapping_rules_intersect_existing_identity_and_reject_unmatched_anonymous_source(app_repo):
    """重叠 CIDR 合并规则权限，随后与账号 scope 相交；未命中在认证前即被拒绝。"""
    _, repo = app_repo
    await repo.db.ip_policy.insert_one({
        "id": POLICY_ID, "enabled": True, "version": 1,
        "rules": [
            {"label": "office", "network": "2001:db8::/32", "scopes": ["tasks:read", "commands:send"]},
            {"label": "vpn", "network": "2001:db8:1::/48", "scopes": ["logs:read"]},
        ],
    })
    request = SimpleNamespace(client=SimpleNamespace(host="2001:db8:1::8"), headers={})
    identity = {"id": "operator", "scopes": ["tasks:read", "logs:read", "tasks:write"], "isAdmin": False}
    narrowed = await apply_ip_permissions(repo, request, identity)
    assert narrowed["scopes"] == ["logs:read", "tasks:read"]
    assert narrowed["isAdmin"] is False

    admin = await apply_ip_permissions(repo, request, {"id": "admin", "scopes": ["*"], "isAdmin": True})
    assert admin["scopes"] == ["commands:send", "logs:read", "tasks:read"]
    assert admin["isAdmin"] is False
    with pytest.raises(HTTPException) as rejected:
        await enforce_ip(repo, SimpleNamespace(client=SimpleNamespace(host="2001:db9::1"), headers={"x-forwarded-for": "2001:db8::8"}))
    assert rejected.value.status_code == 403


@pytest.mark.asyncio
async def test_client_policy_never_reads_resource_or_collector_target_addresses(app_repo):
    """允许的客户端可携带任意设备资源 IP；策略函数不接触设备或采集目标字段。"""
    _, repo = app_repo
    await repo.db.ip_policy.insert_one({
        "id": POLICY_ID, "enabled": True, "version": 1,
        "rules": [{"label": "operator client", "network": "192.0.2.0/24", "scopes": ["tasks:write"]}],
    })
    request = SimpleNamespace(client=SimpleNamespace(host="192.0.2.9"), headers={})
    identity = {"id": "operator", "scopes": ["tasks:write"], "resourceIp": "203.0.113.77",
                "serialServerIp": "2001:db8:ffff::20", "isAdmin": False}
    assert await apply_ip_permissions(repo, request, identity) == identity
