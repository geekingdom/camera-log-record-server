"""内容读取耗时仅附加数值诊断，不改变字节、权限和线程收尾合同。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from camera_logs.logs.api import node_request
from camera_logs.logs.file_reads import FileReads
from fastapi import Response
from fastapi.testclient import TestClient
from test_archive_paging import archive_app


def test_authorized_node_read_reports_each_stage_without_changing_body(tmp_path):
    """完整节点路由保留原响应字段，只有授权成功才返回分阶段耗时。"""
    app, _, _ = archive_app(tmp_path)
    with TestClient(app) as client:
        denied = client.get('/internal/read/first')
        assert denied.status_code == 401
        assert 'server-timing' not in denied.headers
        response = client.get('/internal/read/first', params={'offset': 1, 'limit': 3},
                              headers={'Authorization': 'Bearer probe'})
        assert response.status_code == 200
        assert response.json() == {'fileId': 'first', 'sessionId': 'first', 'data': 'AQID', 'nextOffset': 4}
        metrics = dict(item.strip().split(';dur=') for item in response.headers['server-timing'].split(','))
        assert set(metrics) == {'catalog', 'path', 'queue', 'io', 'encode'}
        assert all(float(value) >= 0 for value in metrics.values())


async def test_file_read_separates_dispatch_wait_from_io(monkeypatch, tmp_path):
    """以受控单调时钟区分执行前等待与线程内文件耗时，不依赖睡眠阈值。"""
    reads = FileReads()
    ticks = iter([10., 12., 15.])
    monkeypatch.setattr('camera_logs.logs.file_reads.perf_counter', lambda: next(ticks))
    monkeypatch.setattr(reads._sources, 'read', lambda *args: b'bytes')
    timings = {}
    try:
        assert await reads.read('identity', tmp_path / 'file.log', None, 0, 5, 5, timings=timings) == b'bytes'
        assert timings == {'queue': 2000., 'io': 3000.}
    finally:
        await reads.close()


async def test_node_proxy_forwards_only_timing_header():
    """转发计时头时不透传内部令牌、Cookie 或其他节点响应头。"""
    upstream = httpx.Response(200, json={'nextOffset': 3},
                             headers={'Server-Timing': 'queue;dur=1.234, io;dur=2.345',
                                      'Set-Cookie': 'internal-only=value'})
    repo = SimpleNamespace(get=AsyncMock(return_value={'url': 'http://node'}),
                           settings=SimpleNamespace(internal_token='internal-test'))
    downstream = Response()
    result = await node_request(repo, 'node', '/internal/read/file',
                                client=SimpleNamespace(get=AsyncMock(return_value=upstream)),
                                downstream=downstream)
    assert result == {'nextOffset': 3}
    assert downstream.headers['server-timing'].startswith(upstream.headers['server-timing'])
    stages = dict(item.strip().split(';dur=') for item in downstream.headers['server-timing'].split(','))
    assert set(stages) == {'queue', 'io', 'api_node', 'api_upstream', 'api_decode'}
    assert all(float(value) >= 0 for value in stages.values())
    assert 'set-cookie' not in downstream.headers
