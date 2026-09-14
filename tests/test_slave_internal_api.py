"""内部主机借用接口仅接受受认证的固定引导请求，并验证运行身份，不能发送任意命令。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from camera_logs.node import slave_routes
from camera_logs.node.slave_routes import install_slave_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_internal_bootstrap_requires_token_and_current_run(monkeypatch):
    """公开访问、未知运行及越界端口都在接触collector之前拒绝。"""
    app = FastAPI()
    database = SimpleNamespace(tasks=SimpleNamespace(find_one=AsyncMock(return_value=None)))
    app.state.worker = SimpleNamespace(repo=SimpleNamespace(db=database))
    install_slave_routes(app, SimpleNamespace(internal_token="internal-test"))
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr("camera_logs.node.slave_routes.bootstrap_on_host", dispatch)
    body = {"taskId": "slave", "runId": "run", "generation": 2, "port": 18080, "bootstrapToken": "a"*32}
    with TestClient(app) as client:
        assert client.post("/internal/slave-ssh/host", json=body).status_code == 401
        headers = {"Authorization": "Bearer internal-test"}
        assert client.post("/internal/slave-ssh/host", json=body, headers=headers).status_code == 409
        assert client.post("/internal/slave-ssh/host", json=body | {"port": 22}, headers=headers).status_code == 422
        assert client.post("/internal/slave-ssh/host", json=body | {"command": "arbitrary"}, headers=headers).status_code == 422
        dispatch.assert_not_awaited()
        database.tasks.find_one.return_value = {"id": "slave", "runId": "run", "generation": 2}
        assert client.post("/internal/slave-ssh/host", json=body, headers=headers).json() == {"bootstrapped": True}
        assert dispatch.call_args.args[2]["_bootstrapToken"] == "a"*32
        assert database.tasks.find_one.call_args.args[0] == {"id": "slave", "runId": "run", "generation": 2}


def test_internal_bootstrap_timeout_cancels_remote_action_and_returns_unknown(monkeypatch):
    """整体超时覆盖主机命令队列等待；调用方据此保留资源租约，不能临时重发。"""
    app = FastAPI()
    database = SimpleNamespace(tasks=SimpleNamespace(find_one=AsyncMock(return_value={"id": "slave", "runId": "run", "generation": 2})))
    app.state.worker = SimpleNamespace(repo=SimpleNamespace(db=database))
    install_slave_routes(app, SimpleNamespace(internal_token="internal-test"))
    cancelled = asyncio.Event()

    async def blocked(*_args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(slave_routes, "REMOTE_BOOTSTRAP_EXECUTION_SECONDS", .01)
    monkeypatch.setattr(slave_routes, "bootstrap_on_host", blocked)
    body = {"taskId": "slave", "runId": "run", "generation": 2, "port": 18080, "bootstrapToken": "a" * 32}
    with TestClient(app) as client:
        response = client.post("/internal/slave-ssh/host", json=body, headers={"Authorization": "Bearer internal-test"})
    assert response.status_code == 503
    assert cancelled.is_set()
