"""第三方服务令牌访问 coredump 的默认权限与实时撤销回归。"""

from __future__ import annotations

from datetime import timedelta

from camera_logs.common.config import Settings
from camera_logs.common.database import now
from camera_logs.coredumps import api as coredump_api
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi import Response
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

BOOTSTRAP = "coredump-permission-bootstrap"
CLIENT_IP = "198.51.100.18"


def _create_user_and_token(client: TestClient, username: str) -> tuple[dict, dict]:
    """经正式管理接口创建 scopes 为空的绑定账号和第三方令牌。"""
    user = client.post("/api/v1/users", json={
        "username": username, "displayName": "第三方 coredump 用户",
        "password": username + "-password-123", "scopes": [],
    })
    assert user.status_code == 201, user.text
    token = client.post("/api/v1/service-tokens", json={
        "name": "coredump 默认读取", "userId": user.json()["id"], "expiresInDays": None,
    })
    assert token.status_code == 201, token.text
    return user.json(), token.json()


def test_service_token_uses_default_coredump_permissions_and_obeys_live_revocation(tmp_path, monkeypatch):
    """普通第三方可读共享 core、创建自己的导出；实时收窄后既有票据也必须失效。"""
    async def fake_proxy(_repo, _node_id, _path, range_header=None, _if_range=None):
        """隔离节点 HTTP，仅断言内容路由已收到完整或 Range 下载请求。"""
        if range_header:
            return Response(b"range", status_code=206, headers={"Content-Range": "bytes 1-5/6"})
        return Response(b"complete")

    monkeypatch.setattr(coredump_api, "proxy_file", fake_proxy)
    settings = Settings(
        _env_file=None, bootstrap_token=BOOTSTRAP, encryption_key=Fernet.generate_key().decode(),
        log_root=tmp_path, node_id="node-a", start_background=False,
    )
    app = create_app(settings, AsyncMongoMockClient().camera_logs)
    with TestClient(app, client=(CLIENT_IP, 46000)) as client:
        client.headers["Authorization"] = "Bearer " + BOOTSTRAP
        repo = client.app.state.repo
        client.portal.call(repo.db.resources.insert_one, {
            "id": "shared-camera", "name": "他人设备", "kind": "HIKVISION_NETWORK",
            "ip": "192.0.2.41", "authenticatedAt": now(), "createdBy": "other-user",
        })
        client.portal.call(repo.db.coredump_files.insert_one, {
            "id": "shared-core", "resourceId": "shared-camera", "nodeId": "node-a", "name": "core.gz",
            "size": 6, "version": 1, "receivedAt": now(), "status": "FROZEN",
            "source": {"size": 6}, "snapshot": {"path": "/private/core.gz"},
        })
        user, token = _create_user_and_token(client, "coredump-default-token")
        headers = {"Authorization": "Bearer " + token["token"], "Idempotency-Key": "third-party-coredump-export"}

        listed = client.get("/api/v1/resources/shared-camera/coredumps", headers=headers)
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["items"]] == ["shared-core"]

        created = client.post("/api/v1/coredump-exports", headers=headers, json={"fileIds": ["shared-core"]})
        assert created.status_code == 202, created.text
        export_id = created.json()["id"]
        assert client.get(f"/api/v1/coredump-exports/{export_id}", headers=headers).status_code == 200
        client.portal.call(repo.db.coredump_exports.update_one, {"id": export_id}, {"$set": {
            "status": "SUCCEEDED", "coordinatorNodeId": "node-a", "expiresAt": now() + timedelta(hours=1),
        }})
        client.portal.call(repo.db.coredump_exports.insert_one, {
            "id": "other-export", "actor": "other-user", "status": "SUCCEEDED", "coordinatorNodeId": "node-a",
            "expiresAt": now() + timedelta(hours=1),
        })
        assert client.get("/api/v1/coredump-exports/other-export", headers=headers).status_code == 403
        assert client.post("/api/v1/coredump-exports/other-export/browser-session", headers=headers).status_code == 403
        assert client.get("/api/v1/coredump-exports/other-export/content", headers=headers).status_code == 403

        # 服务集成直接使用 Bearer，不依赖浏览器 Cookie 或额外登录。
        for path in ("/api/v1/coredumps/shared-core/content", f"/api/v1/coredump-exports/{export_id}/content"):
            full = client.get(path, headers=headers)
            assert full.status_code == 200 and full.content == b"complete"
            ranged = client.get(path, headers=headers | {"Range": "bytes=1-5"})
            assert ranged.status_code == 206 and ranged.headers["Content-Range"] == "bytes 1-5/6"

        client.headers.pop("Authorization")
        core_ticket = client.post("/api/v1/coredumps/shared-core/browser-session", headers=headers)
        assert core_ticket.status_code == 200, core_ticket.text
        assert client.get(core_ticket.json()["url"]).content == b"complete"
        ranged = client.get(core_ticket.json()["url"], headers={"Range": "bytes=1-5"})
        assert ranged.status_code == 206 and ranged.content == b"range"

        export_ticket = client.post(f"/api/v1/coredump-exports/{export_id}/browser-session", headers=headers)
        assert export_ticket.status_code == 200, export_ticket.text
        assert client.get(export_ticket.json()["url"]).content == b"complete"

        client.portal.call(repo.db.ip_policy.insert_one, {
            "id": "platform-ip-policy", "version": 1, "enabled": True,
            "rules": [{"label": "只读任务", "network": "198.51.100.0/24", "scopes": ["tasks:read"]}],
        })
        assert client.get("/api/v1/resources/shared-camera/coredumps", headers=headers).status_code == 403
        assert client.get(core_ticket.json()["url"]).status_code == 403

        client.portal.call(repo.db.ip_policy.update_one, {"id": "platform-ip-policy"}, {"$set": {"enabled": False}})
        client.portal.call(repo.db.users.update_one, {"id": user["id"]}, {"$set": {"enabled": False}})
        assert client.get("/api/v1/resources/shared-camera/coredumps", headers=headers).status_code == 401
        assert client.get(export_ticket.json()["url"]).status_code == 401

        client.portal.call(repo.db.users.update_one, {"id": user["id"]}, {"$set": {"enabled": True, "deletedAt": now()}})
        assert client.get("/api/v1/resources/shared-camera/coredumps", headers=headers).status_code == 401
