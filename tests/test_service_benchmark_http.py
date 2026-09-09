"""压测 HTTP 分池保持请求身份、连接预算及异常时的完整回收。"""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from service_benchmark_http import BenchmarkTransport


def transports(monkeypatch, *, fail_enter=None, fail_close=None):
    """使用不访问网络的传输替身，记录独立预算与资源释放。"""
    instances = []

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self, *, limits):
            self.index, self.limits = len(instances), limits
            self.closed, self.requests = False, []
            instances.append(self)

        async def __aenter__(self):
            if self.index == fail_enter:
                raise OSError("enter failure")
            return self

        async def aclose(self):
            self.closed = True
            if self.index == fail_close:
                raise OSError("close failure")

        async def handle_async_request(self, request):
            self.requests.append(request)
            await asyncio.sleep(0)
            if request.url.path == "/fail":
                raise httpx.ReadError("injected read failure")
            return httpx.Response(200, content=request.content or str(request.url).encode())

    monkeypatch.setattr("service_benchmark_http.httpx.AsyncHTTPTransport", Transport)
    return instances


@pytest.mark.parametrize("routes", [1, 64, 500])
async def test_concurrent_requests_preserve_payload_headers_and_total_budget(monkeypatch, routes):
    instances = transports(monkeypatch)
    async with httpx.AsyncClient(transport=BenchmarkTransport(routes), base_url="http://test",
                                 headers={"Authorization": "Bearer test-only"}, timeout=120) as client:
        replies = await asyncio.gather(*(client.post(f"/tasks/{i}", content=str(i)) for i in range(24)))
        assert [r.text for r in replies] == [str(i) for i in range(24)]
        async with client.stream("GET", "/download", headers={"Range": "bytes=5-9"}) as response:
            assert b"".join([chunk async for chunk in response.aiter_bytes()]) == b"http://test/download"
        assert sum(t.limits.max_connections for t in instances) == max(32, routes + 8)
        assert sum(t.limits.max_keepalive_connections for t in instances) == max(32, routes + 8)
        requests = [r for t in instances for r in t.requests]
        assert all(r.headers["Authorization"] == "Bearer test-only" for r in requests)
        assert all(r.extensions["timeout"]["read"] == 120 for r in requests)
        assert next(r for r in requests if r.url.path == "/download").headers["Range"] == "bytes=5-9"
        assert max(len(t.requests) for t in instances) - min(len(t.requests) for t in instances) <= 1
    assert all(t.closed for t in instances)


async def test_partial_initialization_releases_entered_transports(monkeypatch):
    instances = transports(monkeypatch, fail_enter=3)
    with pytest.raises(OSError, match="enter failure"):
        async with BenchmarkTransport(64):
            pytest.fail("must not enter")
    assert all(t.closed for t in instances[:3])


async def test_failed_request_is_not_retried_and_closed_transport_rejects_reuse(monkeypatch):
    instances = transports(monkeypatch)
    transport = BenchmarkTransport(64)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(httpx.ReadError, match="injected"):
            await client.get("/fail")
    assert sum(len(t.requests) for t in instances) == 1
    assert all(t.closed for t in instances)
    with pytest.raises(RuntimeError, match="未运行"):
        await transport.handle_async_request(httpx.Request("GET", "http://test/closed"))
    with pytest.raises(RuntimeError, match="不能重复启动"):
        await transport.__aenter__()


@pytest.mark.parametrize("cancel", [False, True])
async def test_close_failure_or_cancellation_still_releases_all_transports(monkeypatch, cancel):
    instances = transports(monkeypatch, fail_close=None if cancel else 4)
    with pytest.raises(asyncio.CancelledError if cancel else OSError):
        async with BenchmarkTransport(64):
            if cancel:
                raise asyncio.CancelledError()
    assert all(t.closed for t in instances)
