"""PSH 口令提供器测试：所有 HTTP 调用均由 MockTransport 拦截。"""

import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from camera_logs.collection.psh_passwords import PshPasswordError, PshPasswordProvider


def settings(**values):
    defaults = {
        "psh_mode": "http", "psh_token_url": "https://auth.example/token", "psh_api_url": "https://api.example/",
        "psh_client_id": "client-id", "psh_client_secret": "client-secret", "psh_api_key": "api-key",
        "psh_user_name": "service-user", "psh_mock_password_file": None,
    }
    return SimpleNamespace(**(defaults | values))


def task():
    return {"protocol": "SSH", "ip": "192.0.2.10", "port": 22}


@pytest.mark.asyncio
async def test_disabled_mode_is_the_safe_default():
    provider = PshPasswordProvider(SimpleNamespace(psh_mode="disabled"))
    with pytest.raises(PshPasswordError, match="未启用"):
        await provider(task(), "challenge")


@pytest.mark.asyncio
async def test_mock_reads_file_on_each_call_and_requires_exact_task_key(tmp_path):
    path = tmp_path / "passwords.json"
    path.write_text(json.dumps({"SSH:192.0.2.10:22": "first"}), encoding="utf-8")
    provider = PshPasswordProvider(settings(psh_mode="mock", psh_mock_password_file=path))
    assert await provider(task(), "ignored") == "first"
    path.write_text(json.dumps({"SSH:192.0.2.10:22": "rotated"}), encoding="utf-8")
    assert await provider(task(), "ignored") == "rotated"
    with pytest.raises(PshPasswordError, match="缺失"):
        await provider({"protocol": "SSH", "ip": "192.0.2.11", "port": 22}, "ignored")


@pytest.mark.asyncio
async def test_http_contract_returns_data_data_and_sends_required_headers():
    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.host == "auth.example":
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            assert request.content == b"grant_type=client_credentials&client_id=client-id&client_secret=client-secret"
            return httpx.Response(200, json={"access_token": "token-one"})
        assert request.headers["x-hicode-authorization"] == "Bearer token-one"
        assert request.headers["x-cloudapi-clientid"] == "client-id"
        assert request.headers["x-cloudapi-apikey"] == "api-key"
        assert json.loads(request.content) == {"source": "QmFzZTY0K2NpcGhlcnRleHQ9PQ==", "userName": "service-user"}
        return httpx.Response(200, json={"data": {"data": "password"}})

    provider = PshPasswordProvider(settings(), transport=httpx.MockTransport(handler))
    assert await provider(task(), "QmFzZTY0K2NpcGhlcnRleHQ9PQ==") == "password"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_403003_refreshes_token_once_then_replays_request():
    calls = []

    async def handler(request):
        calls.append(request)
        if request.url.host == "auth.example":
            return httpx.Response(200, json={"access_token": f"token-{sum(x.url.host == 'auth.example' for x in calls)}"})
        if request.headers["x-hicode-authorization"] == "Bearer token-1":
            return httpx.Response(200, json={"code": "403003"})
        return httpx.Response(200, json={"data": {"data": "password"}})

    provider = PshPasswordProvider(settings(), transport=httpx.MockTransport(handler))
    assert await provider(task(), "challenge") == "password"
    assert [request.url.host for request in calls] == ["auth.example", "api.example", "auth.example", "api.example"]


@pytest.mark.asyncio
async def test_successful_token_is_cached_but_each_raw_challenge_is_forwarded_unchanged():
    """同一 Worker 复用有效 OAuth token，但每个设备的完整原始 source 均独立提交。"""
    calls, sources = [], []

    async def handler(request):
        calls.append(request.url.host)
        if request.url.host == "auth.example":
            return httpx.Response(200, json={"access_token": "cached-token"})
        source = json.loads(request.content)["source"]
        sources.append(source)
        assert request.headers["x-hicode-authorization"] == "Bearer cached-token"
        return httpx.Response(200, json={"data": {"data": "password"}})

    provider = PshPasswordProvider(settings(), transport=httpx.MockTransport(handler))
    first, second = "QmFzZTY0K2ZpcnN0PT0=", "QmFzZTY0K3NlY29uZD09"
    assert await provider(task(), first) == "password"
    assert await provider(task(), second) == "password"
    assert calls == ["auth.example", "api.example", "api.example"]
    assert sources == [first, second]


@pytest.mark.asyncio
async def test_second_403003_never_returns_misleading_password_data():
    async def handler(request):
        if request.url.host == "auth.example":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(200, json={"code": "403003", "data": {"data": "must-not-return"}})

    provider = PshPasswordProvider(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(PshPasswordError, match="认证失败"):
        await provider(task(), "challenge")


@pytest.mark.asyncio
@pytest.mark.parametrize("field, value", [
    ("psh_token_url", "http://auth.example/token"),
    ("psh_api_url", "https://user:secret@api.example/"),
])
async def test_http_mode_rejects_insecure_or_secret_bearing_urls(field, value):
    provider = PshPasswordProvider(settings(**{field: value}), transport=httpx.MockTransport(lambda _: None))
    with pytest.raises(PshPasswordError, match="URL 无效"):
        await provider(task(), "challenge")


@pytest.mark.asyncio
async def test_http_errors_and_response_body_never_leak_sensitive_values():
    secret = "client-secret challenge password response-body"

    async def handler(request):
        if request.url.host == "auth.example":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(500, text=secret)

    provider = PshPasswordProvider(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(PshPasswordError) as raised:
        await provider(task(), "challenge")
    assert secret not in str(raised.value)
    assert "challenge" not in str(raised.value)


@pytest.mark.asyncio
async def test_decrypt_http_failure_keeps_safe_response_details_and_redacts_json_log(caplog):
    """服务端错误保留阶段、状态和白名单说明，绝不记录挑战值、口令或令牌。"""
    challenge = "private-device-challenge"
    secret = "decrypted-password-and-token"

    async def handler(request):
        if request.url.host == "auth.example":
            return httpx.Response(200, json={"access_token": "oauth-token"})
        return httpx.Response(503, json={
            "code": "SERVICE_BUSY", "message": "upstream is busy", "error_description": "retry later",
            "data": {"data": secret}, "source": challenge, "token": "response-token",
        })

    caplog.set_level(logging.INFO, logger="camera_logs.collection.psh_passwords")
    provider = PshPasswordProvider(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(PshPasswordError) as raised:
        await provider(task() | {"id": "task-1", "runId": "run-1", "sessionId": "session-1"}, challenge)

    error = raised.value
    assert error.diagnostic["stage"] == "DECRYPT_RESPONSE"
    assert error.diagnostic["httpStatus"] == 503
    assert error.diagnostic["serviceCode"] == "SERVICE_BUSY"
    assert error.diagnostic["message"] == "upstream is busy"
    contexts = [getattr(record, "context", {}) for record in caplog.records]
    assert any(context.get("taskId") == "task-1" and context.get("sessionId") == "session-1" for context in contexts)
    serialized = repr(contexts) + str(error) + repr(error.diagnostic)
    for value in (challenge, secret, "oauth-token", "response-token"):
        assert value not in serialized


@pytest.mark.asyncio
async def test_token_invalid_json_and_decrypt_timeout_have_distinct_safe_diagnostics(caplog):
    """令牌格式失败和解密超时不能塌缩成同一通用错误，且异常正文不外泄。"""
    caplog.set_level(logging.INFO, logger="camera_logs.collection.psh_passwords")

    async def invalid_json(request):
        return httpx.Response(200, text="<html>gateway response with password=do-not-log</html>")

    provider = PshPasswordProvider(settings(), transport=httpx.MockTransport(invalid_json))
    with pytest.raises(PshPasswordError) as invalid:
        await provider(task(), "challenge-not-for-log")
    assert invalid.value.diagnostic["stage"] == "TOKEN_RESPONSE"
    assert invalid.value.diagnostic["reason"] == "INVALID_JSON"

    async def timeout(request):
        if request.url.host == "auth.example":
            return httpx.Response(200, json={"access_token": "never-log-token"})
        raise httpx.ReadTimeout("socket password=do-not-log", request=request)

    provider = PshPasswordProvider(settings(), transport=httpx.MockTransport(timeout))
    with pytest.raises(PshPasswordError) as timed_out:
        await provider(task(), "challenge-not-for-log")
    assert timed_out.value.diagnostic["stage"] == "DECRYPT_REQUEST"
    assert timed_out.value.diagnostic["reason"] == "TIMEOUT"
    assert timed_out.value.diagnostic["exceptionType"] == "ReadTimeout"
    serialized = repr([getattr(record, "context", {}) for record in caplog.records])
    for value in ("do-not-log", "never-log-token", "challenge-not-for-log"):
        assert value not in serialized


@pytest.mark.asyncio
async def test_token_html_failure_is_http_status_with_safe_preview_and_no_secret_leak(caplog):
    """非2xx令牌响应优先按HTTP失败归类，预览用于识别网关但不得回显秘密。"""
    challenge, secret = "challenge-should-not-log", "client-secret gateway-token"

    async def handler(_request):
        return httpx.Response(502, content=f"<html>proxy failure {challenge} {secret}</html>",
                              headers={"content-type": "text/html"})

    caplog.set_level(logging.INFO, logger="camera_logs.collection.psh_passwords")
    provider = PshPasswordProvider(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(PshPasswordError) as raised:
        await provider(task(), challenge)
    details = raised.value.diagnostic
    assert details["stage"] == "TOKEN_RESPONSE"
    assert details["reason"] == "HTTP_STATUS"
    assert details["httpStatus"] == 502
    assert details["responseContentType"] == "text/html"
    assert "proxy failure" in details["responsePreview"]
    serialized = repr(details) + repr([getattr(record, "context", {}) for record in caplog.records])
    assert challenge not in serialized and secret not in serialized


@pytest.mark.asyncio
async def test_config_and_mock_failures_keep_legacy_message_and_emit_diagnostics(caplog):
    """配置与mock错误不能绕过JSON日志诊断，旧有面向用户的中文错误保持可识别。"""
    caplog.set_level(logging.INFO, logger="camera_logs.collection.psh_passwords")
    invalid_url = PshPasswordProvider(settings(psh_token_url="http://auth.example/token"))
    with pytest.raises(PshPasswordError, match="URL 无效") as config_error:
        await invalid_url(task(), "challenge")
    assert config_error.value.diagnostic["stage"] == "CONFIG"
    assert config_error.value.diagnostic["reason"] == "INVALID_URL"

    mock = PshPasswordProvider(settings(psh_mode="mock", psh_mock_password_file=None))
    with pytest.raises(PshPasswordError, match="PSH 口令服务失败") as mock_error:
        await mock(task(), "challenge")
    assert mock_error.value.diagnostic["stage"] == "MOCK"
    assert mock_error.value.diagnostic["reason"] == "FILE_UNCONFIGURED"
    assert any(getattr(record, "context", {}).get("stage") in {"CONFIG", "MOCK"} for record in caplog.records)
