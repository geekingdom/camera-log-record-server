"""请求日志覆盖流式响应结束、响应中断及异常前后的实际 HTTP 状态。"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from camera_logs.common import observability
from camera_logs.common.config import Settings
from camera_logs.common.request_context import current_request_context
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

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
    private_error = "request-event-private-credential"
    insert = AsyncMock(side_effect=OSError(private_error))
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    access_logger = logging.getLogger("camera_logs.access")
    capture = Capture()
    access_logger.addHandler(capture)

    async def app(request_scope, receive, send):
        raise RuntimeError("business failure")

    event_store = SimpleNamespace(insert_one=insert)
    request_scope = scope() | {"path": "/api/v1/write", "raw_path": b"/api/v1/write",
        "app": SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(db=SimpleNamespace(request_events=event_store))))}
    try:
        with pytest.raises(RuntimeError, match="business failure"):
            await observability.RequestLoggingMiddleware(app)(request_scope, None, None)
    finally:
        access_logger.removeHandler(capture)
    insert.assert_awaited_once()
    persistence = [record for record in records if record.getMessage() == "请求事件持久化失败"]
    assert len(persistence) == 1
    assert persistence[0].context["errorType"] == "OSError"
    assert persistence[0].context["errorFrames"]
    assert persistence[0].exc_info is None
    assert private_error not in observability.JsonLineFormatter().format(persistence[0])


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


def test_api_request_events_record_successful_reads_without_request_secrets(client):
    """成功的 API 查询也必须进入请求事件，便于按 requestId 还原完整操作轨迹。"""
    response = client.get("/api/v1/tasks", headers={"X-Request-ID": "successful-read"})
    assert response.status_code == 200, response.text
    event = client.portal.call(
        client.app.state.repo.db.request_events.find_one,
        {"requestId": "successful-read"},
    )
    assert event is not None
    assert event["method"] == "GET"
    assert event["route"] == "/api/v1/tasks"
    assert event["httpStatus"] == 200
    assert event["outcome"] == "SUCCEEDED"
    assert event["responseComplete"] is True
    assert event["responseBytes"] == len(response.content)
    assert "query" not in event and "headers" not in event and "body" not in event


def test_unhandled_api_error_uses_safe_reason_and_type_with_observed_response_send(tmp_path):
    """通用 500 必须经过观察发送链路，排障记录不得泄露异常正文。"""
    secret = "synthetic-private-credential"
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    access_logger = logging.getLogger("camera_logs.access")
    capture = Capture()
    access_logger.addHandler(capture)

    settings = Settings(_env_file=None, bootstrap_token="synthetic-bootstrap",
                        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                        start_background=False)
    app = create_app(settings, AsyncMongoMockClient().camera_logs)

    @app.get("/api/v1/request-observability-unhandled")
    async def unhandled():
        raise ValueError(secret)

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/request-observability-unhandled", headers={
                "X-Request-ID": "unhandled-request-observability",
            })
            event = client.portal.call(client.app.state.repo.db.request_events.find_one, {
                "requestId": "unhandled-request-observability",
            })
    finally:
        access_logger.removeHandler(capture)

    assert response.status_code == 500
    assert event["httpStatus"] == 500
    assert event["outcome"] == "FAILED"
    assert event["responseComplete"] is True
    assert event["reason"] == "服务内部异常"
    assert event["errorType"] == "ValueError"
    assert event["responseBytes"] == len(response.content)
    assert secret not in str(event)
    access = [record for record in records if record.name == "camera_logs.access"]
    assert len(access) == 1
    assert access[0].context["error"]["type"] == "ValueError"
    assert access[0].context["error"]["message"] == "服务内部异常"
    assert access[0].context["error"]["frames"][-1]["function"] == "unhandled"
    assert access[0].exc_info is None
    assert response.headers["X-Request-ID"] == "unhandled-request-observability"
    assert secret not in observability.JsonLineFormatter().format(access[0])


def test_streaming_response_error_after_completed_send_does_not_emit_second_500(tmp_path):
    """首块已经发送后发生的处理错误不得覆盖 200 响应，但事件仍保留失败关联。"""
    secret = "stream-private-credential"
    settings = Settings(_env_file=None, bootstrap_token="synthetic-bootstrap",
                        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                        start_background=False)
    app = create_app(settings, AsyncMongoMockClient().camera_logs)

    @app.get("/api/v1/request-observability-stream")
    async def stream():
        async def chunks():
            yield b"first-"
            raise ValueError(secret)

        return StreamingResponse(chunks())

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/request-observability-stream", headers={
            "X-Request-ID": "request-observability-stream",
        })
        event = client.portal.call(client.app.state.repo.db.request_events.find_one, {
            "requestId": "request-observability-stream",
        })

    assert response.status_code == 200
    assert response.content == b"first-"
    assert event["httpStatus"] == 200
    assert event["responseComplete"] is True
    assert event["responseBytes"] == len(response.content)
    assert event["outcome"] == "FAILED"
    assert event["reason"] == "响应完成后处理失败"
    assert event["errorType"] == "ValueError"
    assert event["errorFrames"][-1]["function"] == "chunks"
    assert secret not in str(event)


def test_completed_accepted_response_with_late_error_is_not_left_pending(tmp_path):
    """202 已被完整发送后出现异常属于失败处理，不能继续显示为异步等待。"""
    settings = Settings(_env_file=None, bootstrap_token="synthetic-bootstrap",
                        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                        start_background=False)
    app = create_app(settings, AsyncMongoMockClient().camera_logs)

    @app.get("/api/v1/request-observability-accepted")
    async def accepted():
        async def chunks():
            yield b"accepted"
            raise RuntimeError("accepted-private-credential")

        return StreamingResponse(chunks(), status_code=202)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/request-observability-accepted", headers={
            "X-Request-ID": "request-observability-accepted",
        })
        event = client.portal.call(client.app.state.repo.db.request_events.find_one, {
            "requestId": "request-observability-accepted",
        })

    assert response.status_code == 202 and response.content == b"accepted"
    assert event["responseComplete"] is True
    assert event["outcome"] == "FAILED"
    assert event["reason"] == "响应完成后处理失败"
    assert event["errorType"] == "RuntimeError"
