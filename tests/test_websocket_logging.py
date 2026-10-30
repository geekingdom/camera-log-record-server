"""WebSocket 访问日志只记录身份与生命周期，不采集首帧令牌或设备正文。"""

import asyncio
import gc
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from camera_logs.common import observability
from camera_logs.common.request_context import current_request_context
from camera_logs.logs.live_subscription import await_live_operation
from fastapi import HTTPException
from starlette.websockets import WebSocketDisconnect
from test_api import client  # noqa: F401


@pytest.mark.parametrize("close_code", [1000, 4401, 4408])
async def test_websocket_close_records_route_actor_and_no_payload(monkeypatch, close_code):
    recorded = Mock()
    monkeypatch.setattr(observability, "logging", SimpleNamespace(getLogger=lambda *_: recorded))
    scope = {"type": "websocket", "path": "/api/v1/tasks/task/logs", "headers": [], "state": {},
             "client": ("198.51.100.9", 443)}
    contexts = []

    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/v1/tasks/{task_id}/logs")
        scope["path_params"] = {"task_id": "task"}
        scope["state"]["actor"] = {"id": "operator", "token": "never-log-identity-secret"}
        contexts.append(current_request_context())
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": "never-log-device-text"})
        await send({"type": "websocket.close", "code": close_code, "reason": "never-log-close-secret"})

    await observability.RequestLoggingMiddleware(app)(scope, AsyncMock(return_value={
        "type": "websocket.receive", "text": '{"token":"never-log-first-frame"}',
    }), AsyncMock())
    assert recorded.log.call_count == 1
    context = recorded.log.call_args.kwargs["extra"]["context"]
    assert context["closeCode"] == close_code
    assert context["actor"] == "operator" and context["requestId"]
    assert context["clientIp"] == "198.51.100.9"
    assert contexts == [{"requestId": context["requestId"], "clientIp": "198.51.100.9"}]
    assert current_request_context() == {}
    assert context["route"] == "/api/v1/tasks/{task_id}/logs"
    assert context["targets"] == {"task_id": "task"}
    assert context["framesSent"] == 1
    assert "never-log" not in str(recorded.mock_calls)


async def test_websocket_exception_is_logged_without_sensitive_message(monkeypatch):
    recorded = Mock()
    monkeypatch.setattr(observability, "logging", SimpleNamespace(getLogger=lambda *_: recorded))

    async def app(*_):
        raise ValueError("never-log-arbitrary-secret")

    with pytest.raises(ValueError):
        await observability.RequestLoggingMiddleware(app)(
            {"type": "websocket", "path": "/logs", "headers": [], "state": {}}, AsyncMock(), AsyncMock(),
        )
    context = recorded.log.call_args.kwargs["extra"]["context"]
    assert context["errorType"] == "ValueError" and context["closeCode"] == 1006
    assert "never-log" not in str(recorded.mock_calls)


def test_bare_bearer_is_redacted():
    assert "sample-secret" not in observability.redact_text("upstream rejected Bearer sample-secret")


@pytest.mark.parametrize("scheme", ["ws", "wss"])
def test_live_endpoint_accepts_plain_and_tls_websocket_schemes(client, scheme):  # noqa: F811
    """ASGI入口两种scheme均能首帧认证并推送；TLS终结由部署反向代理承担。"""
    client.portal.call(client.app.state.repo.db.tasks.insert_one, {"id": "scheme-task", "status": "STOPPED"})
    with client.websocket_connect(f"{scheme}://testserver/api/v1/tasks/scheme-task/logs") as socket:
        socket.send_json({"token": "test-admin-token"})
        assert socket.receive_json() == {"type": "status", "status": "STOPPED"}
        socket.close()


def test_live_subscription_audit_keeps_websocket_request_context_for_service_token(client):  # noqa: F811
    """首帧服务账号认证后的订阅审计必须保留用户、令牌和连接来源关联。"""
    user = client.post("/api/v1/users", json={
        "username": "websocket-audit", "displayName": "实时订阅账号",
        "password": "websocket-audit-password", "scopes": ["logs:read"],
    }).json()
    token = client.post("/api/v1/service-tokens", json={
        "name": "实时订阅令牌", "userId": user["id"], "expiresInDays": 1,
    }).json()
    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.insert_one, {"id": "websocket-audit-task", "status": "STOPPED"})

    with client.websocket_connect("/api/v1/tasks/websocket-audit-task/logs") as socket:
        socket.send_json({"token": token["token"]})
        assert socket.receive_json() == {"type": "status", "status": "STOPPED"}
        socket.close()

    audit = client.portal.call(repo.db.audit.find_one, {
        "action": "live_subscribe", "targetId": "websocket-audit-task",
    })
    assert audit["actor"] == user["id"]
    assert audit["serviceTokenId"] == token["id"]
    assert audit["requestId"]
    assert audit["clientIp"]


def test_real_endpoint_rejection_is_attributed_without_token(client, monkeypatch):  # noqa: F811
    """真实 FastAPI 首帧鉴权拒绝也须通过中间件产生可关联的失败记录。"""
    recorded = Mock()
    monkeypatch.setattr(observability, "logging", SimpleNamespace(getLogger=lambda *_: recorded))
    with client.websocket_connect("/api/v1/tasks/task/logs") as socket:
        socket.send_json({"token": "never-log-invalid-token"})
        with pytest.raises(WebSocketDisconnect) as failure:
            socket.receive_json()
        assert failure.value.code == 4401
    context = recorded.log.call_args.kwargs["extra"]["context"]
    assert context["closeCode"] == 4401 and context["errorType"] == "HTTPException"
    assert context["requestId"] and context["targets"] == {"task_id": "task"}
    assert context["actor"] is None
    assert "never-log-invalid-token" not in str(recorded.mock_calls)


def test_node_unavailable_is_not_reported_as_authentication_failure(client, monkeypatch):  # noqa: F811
    """首帧已认证后节点暂不可用使用 1013，不能误报为需要更换凭据的 4401。"""
    client.portal.call(client.app.state.repo.db.tasks.insert_one, {"id": "task", "nodeId": "node"})
    monkeypatch.setattr("camera_logs.logs.api.node_request", AsyncMock(side_effect=HTTPException(503, "节点暂不可用")))
    with client.websocket_connect("/api/v1/tasks/task/logs") as socket:
        socket.send_json({"token": "test-admin-token"})
        with pytest.raises(WebSocketDisconnect) as failure:
            socket.receive_json()
    assert failure.value.code == 1013


def test_live_disconnect_cancels_inflight_node_tail_immediately(client, monkeypatch):  # noqa: F811
    """浏览器关闭订阅时，未返回的节点轮询也必须被取消，不能继续占用 HTTP 连接。"""
    started, cancelled = Event(), Event()
    client.portal.call(client.app.state.repo.db.tasks.insert_one, {"id": "disconnect-tail", "nodeId": "node"})

    async def blocked_tail(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr("camera_logs.logs.api.node_request", blocked_tail)
    with client.websocket_connect("/api/v1/tasks/disconnect-tail/logs") as socket:
        socket.send_json({"token": "test-admin-token"})
        assert started.wait(1)
        socket.close()
        assert cancelled.wait(1)


async def test_disconnect_wins_over_simultaneous_failed_operation_without_unretrieved_task_error():
    """断开与上游失败同轮完成时，仍以断开收尾并读取失败任务结果。"""
    loop = asyncio.get_running_loop()
    reported = []
    previous_handler = loop.get_exception_handler()
    failed, disconnected = loop.create_future(), loop.create_future()
    failed.set_exception(RuntimeError("simultaneous-tail-failure"))
    disconnected.set_result(1000)
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        with pytest.raises(WebSocketDisconnect) as closed:
            await await_live_operation(failed, disconnected)
        del failed
        gc.collect()
    finally:
        loop.set_exception_handler(previous_handler)
    assert closed.value.code == 1000
    assert reported == []


@pytest.mark.parametrize("stage", ["authentication", "audit", "task-read"])
def test_live_disconnect_cancels_inflight_pre_tail_operation(client, monkeypatch, stage):  # noqa: F811
    """客户端离开也必须取消仍在等待的鉴权、审计或任务读取，不能留下孤儿协程。"""
    started, cancelled = Event(), Event()

    async def blocked(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    token = "test-admin-token"
    if stage == "authentication":
        token = "waiting-authentication"
        monkeypatch.setattr("camera_logs.logs.api.authenticate", blocked)
    elif stage == "audit":
        monkeypatch.setattr(client.app.state.repo, "audit", blocked)
    else:
        monkeypatch.setattr(client.app.state.repo, "get", blocked)

    with client.websocket_connect("/api/v1/tasks/pre-tail-cancel/logs") as socket:
        socket.send_json({"token": token})
        assert started.wait(1)
        socket.close()
        assert cancelled.wait(1)


def test_live_subscription_rechecks_revoked_service_token_before_next_tail(client, monkeypatch):  # noqa: F811
    """令牌撤销后既有订阅不得继续发起下一次节点 tail。"""
    started = Event()
    user = client.post("/api/v1/users", json={
        "username": "live-revoked", "displayName": "实时撤销", "password": "live-revoked-password",
        "scopes": ["logs:read"],
    }).json()
    token = client.post("/api/v1/service-tokens", json={"name": "实时撤销令牌", "userId": user["id"]}).json()
    client.portal.call(client.app.state.repo.db.tasks.insert_one, {"id": "revoked-live", "nodeId": "node"})

    async def empty_tail(*_args, **_kwargs):
        started.set()
        return {"gap": False, "frames": []}

    monkeypatch.setattr("camera_logs.logs.api.node_request", empty_tail)
    with client.websocket_connect("/api/v1/tasks/revoked-live/logs") as socket:
        socket.send_json({"token": token["token"]})
        assert started.wait(1)
        assert client.delete(f'/api/v1/service-tokens/{token["id"]}').status_code == 204
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 4401


@pytest.mark.parametrize("task", [
    {"id": "deleted-live", "deletedAt": "2026-10-30T00:00:00Z", "status": "STOPPED"},
    {"id": "stopped-live", "desiredState": "STOPPED", "status": "STOPPED"},
])
def test_live_subscription_ends_for_deleted_or_fully_stopped_task(client, task):  # noqa: F811
    """软删除和已完成停止都不应保留无界实时订阅；暂停仍由普通状态帧维持。"""
    client.portal.call(client.app.state.repo.db.tasks.insert_one, task)
    with client.websocket_connect(f'/api/v1/tasks/{task["id"]}/logs') as socket:
        socket.send_json({"token": "test-admin-token"})
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 1000


def test_live_subscription_keeps_paused_task_open(client):  # noqa: F811
    """暂停仅表示暂不产生日志，订阅需保留以等待恢复同一运行。"""
    client.portal.call(client.app.state.repo.db.tasks.insert_one, {
        "id": "paused-live", "desiredState": "PAUSED", "status": "PAUSED",
    })
    with client.websocket_connect("/api/v1/tasks/paused-live/logs") as socket:
        socket.send_json({"token": "test-admin-token"})
        assert socket.receive_json() == {"type": "status", "status": "PAUSED"}
        socket.close()


async def test_shutdown_cancellation_collects_simultaneous_upstream_failure():
    """服务器取消与上游失败竞争时保留取消语义，且不留下未读取异常。"""
    loop = asyncio.get_running_loop()
    reported = []
    previous_handler = loop.get_exception_handler()
    failed, disconnected = loop.create_future(), loop.create_future()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        waiting = asyncio.create_task(await_live_operation(failed, disconnected))
        await asyncio.sleep(0)
        failed.set_exception(RuntimeError("shutdown-tail-failure"))
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        del failed, waiting
        gc.collect()
    finally:
        disconnected.cancel()
        loop.set_exception_handler(previous_handler)
    assert reported == []
