"""验证浏览器下载票据的任务隔离、有效期和服务账号撤销行为。

使用最小内容路由隔离文件代理，测试实际 Cookie 签发和鉴权链路，
避免依赖采集节点或把下载令牌打印到测试输出。
"""
import hashlib
from datetime import timedelta
from typing import Annotated

from camera_logs.common.database import now
from camera_logs.common.security import authorize
from camera_logs.logs.download_sessions import download_actor, install_download_sessions
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_api import client  # noqa: F401


def test_ticket_transaction_failure_never_sets_cookie(client, monkeypatch):  # noqa: F811
    """数据库提交不可确认时，不发布任何浏览器授权。"""
    from camera_logs.common import audited_mutations
    from pymongo.errors import ConnectionFailure

    repo = client.app.state.repo
    client.portal.call(repo.db.jobs.insert_one, {
        "id": "unconfirmed", "taskId": "task-a", "status": "SUCCEEDED"})

    async def unavailable(*args):
        raise ConnectionFailure("simulated database failure")

    monkeypatch.setattr(audited_mutations, "mutation_transaction", unavailable)
    result = client.post("/api/v1/downloads/unconfirmed/browser-session")
    assert result.status_code == 503
    assert "set-cookie" not in result.headers
    assert client.portal.call(repo.db.download_sessions.count_documents, {}) == 0


def test_ticket_commit_acknowledgement_loss_recovers_without_reissuing(client, monkeypatch):  # noqa: F811
    """提交已完成但确认丢失，只读恢复原票据，审计和凭据各一份。"""
    from camera_logs.common import audited_mutations
    from pymongo.errors import ConnectionFailure

    repo = client.app.state.repo
    client.portal.call(repo.db.jobs.insert_one, {
        "id": "confirmed", "taskId": "task-a", "status": "SUCCEEDED"})
    original = audited_mutations.mutation_transaction
    calls = []

    async def lost_ack(repo, callback):
        calls.append(True)
        await original(repo, callback)
        raise ConnectionFailure("simulated acknowledgement loss")

    monkeypatch.setattr(audited_mutations, "mutation_transaction", lost_ack)
    result = client.post("/api/v1/downloads/confirmed/browser-session")
    assert result.status_code == 200
    assert len(calls) == 1
    assert "HttpOnly" in result.headers["set-cookie"]
    assert client.portal.call(repo.db.download_sessions.count_documents, {}) == 1
    assert client.portal.call(repo.db.audit.count_documents, {
        "action": "browser_download_authorization", "targetId": "confirmed"}) == 1


def test_cookie_download_is_scoped_revocable_and_expires(client):  # noqa: F811
    repo = client.app.state.repo
    client.portal.call(repo.db.jobs.insert_one, {
        "id": "export-a", "taskId": "task-a", "status": "SUCCEEDED"})
    user = client.post("/api/v1/users", json={
        "username": "download-token", "displayName": "下载令牌用户",
        "password": "download-token-password", "scopes": [],
    }).json()
    identity = client.post("/api/v1/service-tokens", json={
        "name": "download-only", "userId": user["id"]}).json()
    app = FastAPI()
    app.state.repo = repo
    install_download_sessions(app)

    @app.get("/api/v1/downloads/{identifier}/content")
    async def content(request: Request, user: Annotated[dict, Depends(download_actor)]):
        authorize(user, "logs:download", "task-a")
        return {"actor": user["id"]}

    with TestClient(app) as browser:
        result = browser.post("/api/v1/downloads/export-a/browser-session",
            headers={"Authorization": "Bearer " + identity["token"]})
        assert result.status_code == 200
        assert "HttpOnly" in result.headers["set-cookie"]
        assert browser.get(result.json()["url"]).status_code == 200
        assert browser.get("/api/v1/downloads/export-b/content").status_code == 401
        token = browser.cookies.get("download_access")
        client.portal.call(repo.db.download_sessions.update_one,
            {"tokenHash": hashlib.sha256(token.encode()).hexdigest()},
            {"$set": {"expiresAt": now() - timedelta(seconds=1)}})
        assert browser.get(result.json()["url"]).status_code == 401
        browser.post("/api/v1/downloads/export-a/browser-session",
            headers={"Authorization": "Bearer " + identity["token"]})
        client.delete("/api/v1/service-tokens/" + identity["id"])
        assert browser.get(result.json()["url"]).status_code == 401


def test_login_cookie_download_is_revoked_by_ip_scope_and_account_disable(tmp_path, monkeypatch):
    """正式下载路由接受登录会话，且策略收窄或停用账号后立即拒绝同一 Cookie。"""
    from camera_logs.common.config import Settings
    from camera_logs.common.database import now
    from camera_logs.main import create_app
    from cryptography.fernet import Fernet
    from mongomock_motor import AsyncMongoMockClient

    async def fake_proxy(*_args, **_kwargs):
        """避免测试访问采集节点，只证明下载授权和路由鉴权链路。"""
        return JSONResponse({"download": "ok"})

    monkeypatch.setattr("camera_logs.logs.api.proxy_file", fake_proxy)
    settings = Settings(_env_file=None, bootstrap_token="download-policy-bootstrap",
                        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                        admin_password="download-admin-password", start_background=False)
    app = create_app(settings, AsyncMongoMockClient().camera_logs)
    csrf = {"X-Requested-With": "XMLHttpRequest"}
    with TestClient(app, client=("198.51.100.18", 46000)) as browser:
        repo = browser.app.state.repo
        browser.portal.call(repo.db.jobs.insert_one, {
            "id": "cookie-download", "taskId": "task-a", "nodeId": "test-node",
            "status": "SUCCEEDED", "kind": "DOWNLOAD", "actor": "other",
            "expiresAt": now() + timedelta(hours=1),
        })
        created = browser.post("/api/v1/users", headers={"Authorization": "Bearer download-policy-bootstrap"}, json={
            "username": "download-user", "displayName": "下载用户", "password": "download-user-password",
            "scopes": ["logs:download"],
        })
        assert created.status_code == 201, created.text
        user = created.json()
        assert browser.post("/api/v1/auth/login", headers=csrf, json={
            "username": "download-user", "password": "download-user-password",
        }).status_code == 200
        assert browser.post("/api/v1/auth/password", headers=csrf, json={
            "currentPassword": "download-user-password", "newPassword": "download-user-password-next",
        }).status_code == 200

        ticket = browser.post("/api/v1/downloads/cookie-download/browser-session", headers=csrf)
        assert ticket.status_code == 200, ticket.text
        assert browser.get(ticket.json()["url"]).json() == {"download": "ok"}

        policy = browser.patch("/api/v1/admin/ip-policy", headers={
            "Authorization": "Bearer download-policy-bootstrap",
        }, json={
            "version": 1, "enabled": True,
            "rules": [{"label": "office", "network": "198.51.100.0/24", "scopes": ["admin"]}],
        })
        assert policy.status_code == 200, policy.text
        assert browser.get(ticket.json()["url"]).status_code == 403

        disabled = browser.patch("/api/v1/admin/ip-policy", headers={
            "Authorization": "Bearer download-policy-bootstrap",
        }, json={"version": policy.json()["version"], "enabled": False, "rules": []})
        assert disabled.status_code == 200, disabled.text
        deleted = browser.delete(f"/api/v1/users/{user['id']}?version=2", headers={
            "Authorization": "Bearer download-policy-bootstrap",
        })
        assert deleted.status_code == 204, deleted.text
        assert browser.get(ticket.json()["url"]).status_code == 401


def test_coredump_browser_ticket_is_path_scoped_and_downloads_natively(client, monkeypatch):  # noqa: F811
    """coredump 的浏览器票据只能访问已成功且未过期的同一导出内容 URL。"""
    from camera_logs.coredumps import api as coredump_api

    async def fake_proxy(*_args, **_kwargs):
        return JSONResponse({"download": "coredump"})

    monkeypatch.setattr(coredump_api, "proxy_file", fake_proxy)
    repo = client.app.state.repo
    client.portal.call(repo.db.coredump_exports.insert_one, {
        "id": "core-export", "actor": "bootstrap", "status": "SUCCEEDED", "coordinatorNodeId": "test-node",
        "resultPath": "/not-exposed", "expiresAt": now() + timedelta(hours=1),
    })
    ticket = client.post("/api/v1/coredump-exports/core-export/browser-session")
    assert ticket.status_code == 200, ticket.text
    assert ticket.json()["url"] == "/api/v1/coredump-exports/core-export/content"
    assert "HttpOnly" in ticket.headers["set-cookie"]
    client.headers.pop("Authorization", None)
    assert client.get(ticket.json()["url"]).json() == {"download": "coredump"}
    # Cookie Path 不匹配时不会发送，且已移除 Bearer，另一对象必须无法借用票据。
    assert client.get("/api/v1/coredump-exports/other/content").status_code == 401
