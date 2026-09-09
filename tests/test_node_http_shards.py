"""节点 HTTP 分池必须保持总预算、请求身份与完整关闭语义。"""

import asyncio

import httpx
import pytest
from camera_logs.common.node_http import NodeHttpPool


def factory(monkeypatch, *, fail_enter=None, fail_close=None):
    """用独立客户端记录分配与退出，失败注入不访问网络。"""
    clients = []

    class Client:
        def __init__(self, **kwargs):
            self.index = len(clients)
            self.options = kwargs
            self.closed = False
            self.requests = []
            clients.append(self)

        async def __aenter__(self):
            if self.index == fail_enter:
                raise RuntimeError('enter failure')
            return self

        async def __aexit__(self, *args):
            self.closed = True
            if self.index == fail_close:
                raise RuntimeError('close failure')

        async def get(self, url, **kwargs):
            self.requests.append((url, kwargs))
            await asyncio.sleep(0)
            return httpx.Response(200, json={'url': url, 'offset': kwargs['params']['offset']})

    monkeypatch.setattr('camera_logs.common.node_http.httpx.AsyncClient', Client)
    return clients


async def test_shards_distribute_requests_without_raising_total_connection_budget(monkeypatch):
    clients = factory(monkeypatch)
    pool = NodeHttpPool()
    async with pool:
        assert len(clients) == 8
        assert sum(c.options['limits'].max_connections for c in clients) == 500
        assert sum(c.options['limits'].max_keepalive_connections for c in clients) == 100
        responses = await asyncio.gather(*(pool.get(f'http://node/read/{i}', params={'offset': i},
                                                  headers={'Authorization': 'Bearer internal-test'})
                                           for i in range(24)))
        assert [r.json()['offset'] for r in responses] == list(range(24))
        assert all(len(c.requests) == 3 for c in clients)
        assert all(c.options['timeout'] == 30 for c in clients)
        assert all(kwargs['headers']['Authorization'] == 'Bearer internal-test'
                   for c in clients for _, kwargs in c.requests)
        assert not pool.is_closed
    assert pool.is_closed and all(c.closed for c in clients)
    with pytest.raises(RuntimeError):
        await pool.get('http://node/read/closed')


async def test_partial_startup_failure_closes_all_entered_clients(monkeypatch):
    clients = factory(monkeypatch, fail_enter=3)
    pool = NodeHttpPool()
    with pytest.raises(RuntimeError, match='enter failure'):
        async with pool:
            pytest.fail('startup should fail')
    assert all(c.closed for c in clients[:3])
    assert pool.is_closed


async def test_one_close_failure_does_not_skip_other_clients(monkeypatch):
    clients = factory(monkeypatch, fail_close=4)
    pool = NodeHttpPool()
    with pytest.raises(RuntimeError, match='close failure'):
        async with pool:
            pass
    assert pool.is_closed and all(c.closed for c in clients)


async def test_cancellation_closes_all_shards(monkeypatch):
    clients = factory(monkeypatch)
    pool = NodeHttpPool()
    with pytest.raises(asyncio.CancelledError):
        async with pool:
            raise asyncio.CancelledError()
    assert pool.is_closed and all(c.closed for c in clients)
