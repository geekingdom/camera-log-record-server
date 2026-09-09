"""请求日志覆盖流式响应结束、响应中断及异常前后的实际 HTTP 状态。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from camera_logs.common import observability
from camera_logs.common.request_context import current_request_context
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

pytest_plugins = ("test_api",)


def scope():
    """创建不包含真实凭据的最小 HTTP ASGI 请求。"""
    return {"type": "http", "asgi": {"version": "3.0"}, "method": "GET",
        "path": "/download", "raw_path": b"/download", "query_string": b"",
        "headers": [], "scheme": "http", "server": ("test", 80), "state": {}}


async def test_logs_only_after_entire_stream_finishes(monkeypatch):
    recorded = Mock()
    monkeypatch.setattr(observability, "log_request", recorded)
    messages = []

    async def app(request_scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"abc", "more_body": True})
        assert not recorded.called
        await send({"type": "http.response.body", "body": b"def", "more_body": False})

    async def send(message):
        messages.append(message)

    await observability.RequestLoggingMiddleware(app)(scope(), None, send)
    recorded.assert_called_once()
    assert recorded.call_args.kwargs["status"] == 200
    assert recorded.call_args.kwargs["response_complete"] is True
    assert recorded.call_args.kwargs["response_bytes"] == 6
    assert dict(messages[0]["headers"])[b"x-request-id"]


@pytest.mark.parametrize("after_start", [False, True])
async def test_stream_failure_is_logged_once_and_propagated(monkeypatch, after_start):
    recorded = Mock()
    monkeypatch.setattr(observability, "log_request", recorded)
    failure = OSError("stream read failed")

    async def app(request_scope, receive, send):
        if after_start:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"abc", "more_body": True})
        raise failure

    async def send(message):
        pass

    with pytest.raises(OSError, match="stream read failed"):
        await observability.RequestLoggingMiddleware(app)(scope(), None, send)
    recorded.assert_called_once()
    result = recorded.call_args.kwargs
    assert result["status"] == (200 if after_start else 500)
    assert result["response_complete"] is False
    assert result["error"] is failure


async def test_cancelled_request_is_not_logged_as_success(monkeypatch):
    recorded = Mock()
    monkeypatch.setattr(observability, "log_request", recorded)

    async def app(request_scope, receive, send):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await observability.RequestLoggingMiddleware(app)(scope(), None, None)
    recorded.assert_called_once()
    assert recorded.call_args.kwargs["status"] == 499
    assert recorded.call_args.kwargs["response_complete"] is False


async def test_failed_send_does_not_count_unconfirmed_bytes(monkeypatch):
    recorded = Mock()
    monkeypatch.setattr(observability, "log_request", recorded)

    async def app(request_scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"abc", "more_body": True})
        await send({"type": "http.response.body", "body": b"unconfirmed", "more_body": False})

    async def send(message):
        if message.get("body") == b"unconfirmed":
            raise OSError("client disconnected")

    with pytest.raises(OSError, match="client disconnected"):
        await observability.RequestLoggingMiddleware(app)(scope(), None, send)
    recorded.assert_called_once()
    assert recorded.call_args.kwargs["response_bytes"] == 3
    assert recorded.call_args.kwargs["response_complete"] is False


async def test_request_context_is_isolated_and_reset_after_each_request():
    """请求关联字段只在当前 ASGI 调用可见，结束后不能污染后台或下一请求。"""
    observed = []

    async def app(request_scope, receive, send):
        observed.append(current_request_context())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    middleware = observability.RequestLoggingMiddleware(app)
    first, second = scope(), scope()
    first["headers"] = [(b"x-request-id", b"first")]
    second["headers"] = [(b"x-request-id", b"second")]
    await middleware(first, None, lambda message: asyncio.sleep(0))
    await middleware(second, None, lambda message: asyncio.sleep(0))

    assert observed == [{"requestId": "first", "clientIp": None}, {"requestId": "second", "clientIp": None}]
    assert current_request_context() == {}


async def test_request_event_persistence_failure_does_not_mask_business_failure():
    """排障记录 Mongo 故障只能降级为日志，原始业务异常仍需完整传播。"""
    insert = AsyncMock(side_effect=OSError("request event unavailable"))

    async def app(request_scope, receive, send):
        raise RuntimeError("business failure")

    event_store = SimpleNamespace(insert_one=insert)
    request_scope = scope() | {"path": "/api/v1/write", "raw_path": b"/api/v1/write",
        "app": SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(db=SimpleNamespace(request_events=event_store))))}
    with pytest.raises(RuntimeError, match="business failure"):
        await observability.RequestLoggingMiddleware(app)(request_scope, None, None)
    insert.assert_awaited_once()


def test_rejected_requests_keep_http_status_and_request_id(monkeypatch):
    recorded = Mock()
    monkeypatch.setattr(observability, "log_request", recorded)
    app = FastAPI()
    observability.add_request_logging(app)

    @app.get("/denied")
    async def denied():
        raise HTTPException(401, "authentication required")

    @app.get("/validated")
    async def validated(count: int):
        return {"count": count}

    with TestClient(app) as client:
        for route, expected in (("/denied", 401), ("/validated?count=invalid", 422)):
            recorded.reset_mock()
            response = client.get(route, headers={"X-Request-ID": "test-correlation"})
            assert response.status_code == expected
            assert response.headers["X-Request-ID"] == "test-correlation"
            recorded.assert_called_once()
            assert recorded.call_args.kwargs["status"] == expected
            assert recorded.call_args.kwargs["response_complete"] is True
            assert recorded.call_args.kwargs["response_bytes"] == len(response.content)


def test_api_request_events_record_write_requests_and_safe_4xx_reason(client):
    """写请求和校验失败均进入排障集合，原因来自异常处理器的安全文本。"""
    assert client.post("/api/v1/no-such-route").status_code == 404
    assert client.get("/api/v1/audit-events?page=0").status_code == 422
    repo = client.app.state.repo
    write = client.portal.call(repo.db.request_events.find_one, {"method": "POST", "route": "/api/v1/no-such-route"})
    validation = client.portal.call(repo.db.request_events.find_one, {"httpStatus": 422})
    assert write is not None
    assert validation["reason"] == "输入校验失败"
