"""PSH 口令提供器测试：所有 HTTP 调用均由 MockTransport 拦截。"""

import json
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
