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
from fastapi.testclient import TestClient
from test_api import client  # noqa: F401


def test_cookie_download_is_scoped_revocable_and_expires(client):  # noqa: F811
    repo = client.app.state.repo
    client.portal.call(repo.db.jobs.insert_one, {
        "id": "export-a", "taskId": "task-a", "status": "SUCCEEDED"})
    identity = client.post("/api/v1/service-tokens", json={
        "name": "download-only", "scopes": ["logs:download"], "taskIds": ["task-a"]}).json()
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
