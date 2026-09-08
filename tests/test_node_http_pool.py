"""高频节点转发复用生命周期客户端，保留鉴权、异常映射和关闭语义。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from camera_logs.common.config import Settings
from camera_logs.logs.api import node_request
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient


async def test_node_requests_reuse_injected_client_and_current_node_address(monkeypatch):
    """请求复用传输层，但每次仍查询最新节点地址并携带内部鉴权。"""
    client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, json={"frames": []})))
    repo = SimpleNamespace(get=AsyncMock(side_effect=[{"url": "http://node-a"}, {"url": "http://node-b"}]),
                           settings=SimpleNamespace(internal_token="internal-test"))

    def unexpected_client(*args, **kwargs):
        raise AssertionError("不得为每个节点请求重建客户端或 TLS 上下文")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)
    for _ in range(2):
        assert await node_request(repo, "node", "/internal/tail/task", client=client) == {"frames": []}
    assert [call.args[0] for call in client.get.call_args_list] == [
        "http://node-a/internal/tail/task", "http://node-b/internal/tail/task"]
    assert all(call.kwargs["headers"]["Authorization"] == "Bearer internal-test" for call in client.get.call_args_list)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("never-log-exception-secret"), httpx.Response(404)])
async def test_pooled_node_errors_remain_retryable(failure, caplog):
    """复用客户端不改变节点失联或文件缺失的正式 503 错误合同。"""
    get = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
    repo = SimpleNamespace(get=AsyncMock(return_value={"url": "http://node"}),
                           settings=SimpleNamespace(internal_token="internal-test"))
    with pytest.raises(HTTPException) as error:
        await node_request(repo, "node", "/internal/read/file", client=SimpleNamespace(get=get))
    assert error.value.status_code == 503
    assert get.await_count == 1
    if isinstance(failure, Exception):
        assert "ReadTimeout" in caplog.text
        assert "never-log-exception-secret" not in caplog.text


@pytest.mark.parametrize("failure", [httpx.ReadError("reset"), httpx.RemoteProtocolError("closed")])
async def test_interrupted_read_retries_same_cursor_once(failure):
    """尚未得到完整响应的只读请求只重试一次，不推进游标或重复交付正文。"""
    client = SimpleNamespace(get=AsyncMock(side_effect=[failure, httpx.Response(200, json={"nextOffset": 9})]))
    repo = SimpleNamespace(get=AsyncMock(return_value={"url": "http://node"}),
                           settings=SimpleNamespace(internal_token="internal-test"))
    result = await node_request(repo, "node", "/internal/read/file", {"offset": 6, "limit": 3}, client=client)
    assert result == {"nextOffset": 9}
    assert client.get.await_count == 2
    assert client.get.call_args_list[0] == client.get.call_args_list[1]


async def test_repeated_read_error_is_not_retried_without_bound():
    """第二次连接读取失败返回 503，不能无限重试或把失败当成空日志。"""
    client = SimpleNamespace(get=AsyncMock(side_effect=httpx.ReadError("reset")))
    repo = SimpleNamespace(get=AsyncMock(return_value={"url": "http://node"}),
                           settings=SimpleNamespace(internal_token="internal-test"))
    with pytest.raises(HTTPException) as error:
        await node_request(repo, "node", "/internal/read/file", client=client)
    assert error.value.status_code == 503
    assert client.get.await_count == 2


async def test_api_lifespan_owns_and_closes_node_client(tmp_path):
    """每个 API 实例独立持有客户端，正常及异常退出均释放连接池。"""
    settings = Settings(encryption_key=Fernet.generate_key().decode(), bootstrap_token="test",
                        log_root=tmp_path, start_background=False)
    app = create_app(settings, AsyncMongoMockClient().db)
    async with app.router.lifespan_context(app):
        first = app.state.node_http
        assert not first.is_closed
    assert first.is_closed
    with pytest.raises(RuntimeError, match="shutdown-test"):
        async with app.router.lifespan_context(app):
            second = app.state.node_http
            assert first is not second
            raise RuntimeError("shutdown-test")
    assert second.is_closed
