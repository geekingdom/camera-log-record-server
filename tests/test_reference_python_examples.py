"""Python 调用示例必须来自目录最终操作合同，并可在替身客户端中执行。"""
# ruff: noqa: F811

import ast
import asyncio
import sys
import types

import pytest
from camera_logs.reference.python_examples import build_python_example
from fastapi.routing import APIRoute
from pydantic import TypeAdapter
from test_api import client  # noqa: F401


def operation(**overrides):
    """构造最小目录操作，真实目录会传入相同的最终字段。"""
    value = {
        "method": "POST",
        "path": "/api/v1/tasks/{task_id}/commands",
        "headers": {"Authorization": "Bearer <SERVICE_TOKEN>", "Idempotency-Key": "example-key"},
        "parameters": [{"name": "task_id", "in": "path", "example": "task/example"},
                       {"name": "verbose", "in": "query", "example": True, "required": True}],
        "requestExample": {"command": "ls", "password": "not-a-real-secret"},
        "responseExample": {"id": "command-example"},
        "responseStatus": 202,
    }
    return value | overrides


class Response:
    """记录请求执行结果，避免示例测试访问任何实际服务。"""

    status_code = 200
    content = b"chunk-onechunk-two"

    def __init__(self):
        self.raised = False

    def raise_for_status(self):
        self.raised = True

    def close(self):
        self.closed = True

    def json(self):
        return {"ok": True}

    @property
    def text(self):
        return "# TYPE camera_tasks gauge\ncamera_tasks 2\n"

    def iter_content(self, chunk_size):
        assert chunk_size == 1024 * 1024
        return [b"chunk-one", b"chunk-two"]


class Session:
    """模拟 requests.Session，保留示例向库传递的完整请求合同。"""

    def __init__(self):
        self.headers = {}
        self.calls = []
        self.response = Response()

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def close(self):
        self.closed = True


def run_http_example(monkeypatch, source):
    """向运行示例注入 requests 替身，返回会话和模块命名空间。"""
    session = Session()
    requests = types.SimpleNamespace(Session=lambda: session)
    monkeypatch.setitem(sys.modules, "requests", requests)
    namespace = {"__name__": "not_main"}
    exec(compile(source, "<python-example>", "exec"), namespace)  # noqa: S102 - 执行生成示例的契约测试。
    namespace["main"]()
    return session, namespace


def test_http_example_is_parseable_and_uses_quoted_path_bearer_params_json_and_errors(monkeypatch):
    """普通 HTTP 示例必须替换路径、传查询和 JSON，且口令不使用目录中的伪值。"""
    monkeypatch.setenv("CAMERA_LOGS_BASE_URL", "https://camera.example")
    monkeypatch.setenv("CAMERA_LOGS_TOKEN", "service-token")
    monkeypatch.setenv("CAMERA_LOGS_IDEMPOTENCY_KEY", "stable-request-key")
    monkeypatch.setenv("CAMERA_LOGS_PASSWORD", "environment-secret")
    source = build_python_example(operation())

    ast.parse(source)
    assert "not-a-real-secret" not in source
    session, _ = run_http_example(monkeypatch, source)
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://camera.example/api/v1/tasks/task%2Fexample/commands"
    assert call["headers"] == {"Authorization": "Bearer service-token", "Idempotency-Key": "stable-request-key"}
    assert call["params"] == {"verbose": True}
    assert call["json"] == {"command": "ls", "password": "environment-secret"}
    assert call["timeout"] == 30 and session.response.raised and session.response.closed and session.closed


def test_session_examples_login_password_and_logout_use_cookie_session_headers(monkeypatch):
    """登录、改密和登出通过同一 Session 处理 Cookie，变更请求带 X-Requested-With。"""
    monkeypatch.setenv("CAMERA_LOGS_BASE_URL", "http://camera.example")
    monkeypatch.setenv("CAMERA_LOGS_USERNAME", "operator")
    monkeypatch.setenv("CAMERA_LOGS_PASSWORD", "environment-secret")
    monkeypatch.setenv("CAMERA_LOGS_CURRENT_PASSWORD", "old-secret")
    monkeypatch.setenv("CAMERA_LOGS_NEW_PASSWORD", "new-secret")
    for path, method, expected_body, status, expected_headers in (
        ("/api/v1/auth/login", "POST", {"username": "operator", "password": "environment-secret"}, 204,
         {"X-Requested-With": "XMLHttpRequest"}),
        ("/api/v1/auth/password", "POST", {"currentPassword": "old-secret", "newPassword": "new-secret"}, 204,
         {"X-Requested-With": "XMLHttpRequest"}),
        ("/api/v1/auth/logout", "POST", None, 204, {"X-Requested-With": "XMLHttpRequest"}),
        ("/api/v1/auth/me", "GET", None, 200, {}),
    ):
        source = build_python_example(operation(method=method, path=path, headers={}, parameters=[],
                                                requestExample=expected_body, responseStatus=status,
                                                responseExample=None))
        ast.parse(source)
        session, _ = run_http_example(monkeypatch, source)
        assert len(session.calls) == (1 if path.endswith("/login") else 2)
        call = session.calls[-1]
        assert call["headers"] == expected_headers
        assert call["json"] == expected_body
        assert session.response.raised


def test_binary_download_streams_to_file_and_uses_success_handling(monkeypatch, tmp_path):
    """二进制下载示例必须使用 stream 和 iter_content，不能把文件正文装入内存。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CAMERA_LOGS_BASE_URL", "https://camera.example")
    monkeypatch.setenv("CAMERA_LOGS_TOKEN", "service-token")
    monkeypatch.setenv("CAMERA_LOGS_IDEMPOTENCY_KEY", "stable-request-key")
    source = build_python_example(operation(method="GET", path="/api/v1/downloads/{identifier}/content",
                                            parameters=[{"name": "identifier", "in": "path", "example": "job-example"}],
                                            requestExample=None, responseExample="<二进制 .tar.gz>", responseStatus=200))
    ast.parse(source)
    session, _ = run_http_example(monkeypatch, source)
    assert session.calls[0]["stream"] is True
    assert (tmp_path / "download.bin").read_bytes() == b"chunk-onechunk-two"
    assert session.response.raised and session.response.closed and session.closed


def test_interrupted_binary_download_keeps_partial_file_and_closes_response(monkeypatch, tmp_path):
    """流式写入中断时只保留 .partial，不能把不完整内容命名为下载完成文件。"""
    class InterruptedResponse(Response):
        def iter_content(self, *, chunk_size):
            assert chunk_size == 1024 * 1024
            yield b"first"
            raise OSError("simulated connection loss")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CAMERA_LOGS_BASE_URL", "https://camera.example")
    monkeypatch.setenv("CAMERA_LOGS_TOKEN", "service-token")
    monkeypatch.setenv("CAMERA_LOGS_IDEMPOTENCY_KEY", "stable-request-key")
    source = build_python_example(operation(method="GET", path="/api/v1/downloads/{identifier}/content",
                                            parameters=[{"name": "identifier", "in": "path", "example": "job-example"}],
                                            requestExample=None, responseExample="<二进制 .tar.gz>", responseStatus=200))
    session = Session()
    session.response = InterruptedResponse()
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(Session=lambda: session))
    namespace = {"__name__": "not_main"}
    exec(compile(source, "<partial-download>", "exec"), namespace)  # noqa: S102 - 执行生成示例的契约测试。
    with pytest.raises(OSError, match="connection loss"):
        namespace["main"]()
    assert (tmp_path / "download.bin.partial").read_bytes() == b"first"
    assert not (tmp_path / "download.bin").exists()
    assert session.response.closed and session.closed


def test_metrics_example_prints_prometheus_text_without_json_decoding(monkeypatch):
    """/metrics 是 Prometheus 文本，示例不能调用 response.json()。"""
    monkeypatch.setenv("CAMERA_LOGS_BASE_URL", "http://camera.example")
    source = build_python_example(operation(method="GET", path="/metrics", headers={}, parameters=[],
                                            requestExample=None, responseExample="# TYPE camera_tasks gauge",
                                            responseStatus=200))
    ast.parse(source)
    assert "response.text" in source and "response.json()" not in source
    session, _ = run_http_example(monkeypatch, source)
    assert session.response.raised and session.calls[0]["url"] == "http://camera.example/metrics"


def test_every_current_catalog_operation_generates_parseable_standalone_source(client):
    """当前目录全部 HTTP 与 WS 项都由同一生成器覆盖，不允许存在无 Python 示例的路径。"""
    operations = client.get("/api/v1/api-reference").json()["operations"]
    assert any(current["method"] == "WS" for current in operations)
    for current in operations:
        source = build_python_example(current)
        assert current["pythonExample"] == source
        ast.parse(source)
        assert "def main" in source


def test_every_catalog_http_example_executes_and_matches_current_body_and_query_contract(client, monkeypatch, tmp_path):
    """每个 HTTP 片段在替身 requests 中可执行，最终 body 和默认查询参数仍通过当前路由模型。"""
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "CAMERA_LOGS_BASE_URL": "http://camera.example",
        "CAMERA_LOGS_TOKEN": "service-token",
        "CAMERA_LOGS_USERNAME": "operator",
        "CAMERA_LOGS_PASSWORD": "environment-secret",
        "CAMERA_LOGS_CURRENT_PASSWORD": "old-secret",
        "CAMERA_LOGS_NEW_PASSWORD": "new-secret",
        "CAMERA_LOGS_IDEMPOTENCY_KEY": "stable-request-key",
    }.items():
        monkeypatch.setenv(name, value)
    routes = {
        (method, route.path): route
        for route in client.app.routes if isinstance(route, APIRoute)
        for method in route.methods
    }
    for current in client.get("/api/v1/api-reference").json()["operations"]:
        if current["method"] == "WS":
            continue
        session, _ = run_http_example(monkeypatch, current["pythonExample"])
        call = session.calls[-1]
        route = routes[(current["method"], current["path"])]
        if route.body_field:
            TypeAdapter(route.body_field.field_info.annotation).validate_python(call["json"])
        query_fields = {field.name: field for field in route.dependant.query_params}
        for name, value in call["params"].items():
            _, errors = query_fields[name].validate(value, {}, loc=("query", name))
            assert not errors, f"{current['id']} query {name}: {errors}"
        assert session.response.raised and session.response.closed and session.closed


def test_websocket_example_sends_authentication_first_frame(monkeypatch):
    """实时日志示例按服务端合同在十秒窗口内发送 token/cursor 首帧。"""
    monkeypatch.setenv("CAMERA_LOGS_BASE_URL", "https://camera.example")
    monkeypatch.setenv("CAMERA_LOGS_TOKEN", "service-token")
    sent, connected = [], []

    class ConnectionClosed(Exception):
        """替身连接关闭。"""

    class Socket:
        def __init__(self):
            self.events = iter(['{"type":"data", "data":"bG9nCg==", "cursor":"next-cursor"}',
                                '{"type":"gap", "cursor":"next-cursor"}'])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send(self, payload):
            sent.append(payload)

        async def recv(self):
            try:
                return next(self.events)
            except StopIteration as error:
                raise ConnectionClosed() from error

    def connect(url, **kwargs):
        connected.append((url, kwargs))
        return Socket()

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=connect, ConnectionClosed=ConnectionClosed))
    source = build_python_example(operation(method="WS", path="/api/v1/tasks/{task_id}/logs",
                                            headers={}, parameters=[{"name": "task_id", "in": "path", "example": "task/example"}],
                                            requestExample={"token": "<SERVICE_TOKEN>", "cursor": "cursor-example"},
                                            responseExample={"type": "data"}, responseStatus=101))
    ast.parse(source)
    namespace = {"__name__": "not_main"}
    exec(compile(source, "<ws-example>", "exec"), namespace)  # noqa: S102 - 执行生成示例的契约测试。
    asyncio.run(namespace["main"]())
    assert connected == [("wss://camera.example/api/v1/tasks/task%2Fexample/logs", {"open_timeout": 10})]
    assert sent == ['{"token": "service-token", "cursor": "cursor-example"}']
