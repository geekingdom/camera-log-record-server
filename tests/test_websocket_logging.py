"""WebSocket 访问日志只记录身份与生命周期，不采集首帧令牌或设备正文。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from camera_logs.common import observability
from fastapi import HTTPException
from starlette.websockets import WebSocketDisconnect
from test_api import client  # noqa: F401


@pytest.mark.parametrize("close_code", [1000, 4401, 4408])
async def test_websocket_close_records_route_actor_and_no_payload(monkeypatch, close_code):
    recorded = Mock()
    monkeypatch.setattr(observability, "logging", SimpleNamespace(getLogger=lambda *_: recorded))
    scope = {"type": "websocket", "path": "/api/v1/tasks/task/logs", "headers": [], "state": {}}

    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/v1/tasks/{task_id}/logs")
        scope["path_params"] = {"task_id": "task"}
        scope["state"]["actor"] = {"id": "operator", "token": "never-log-identity-secret"}
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
