"""本地与远端全文候选证据必须只减少扫描量，不能排除不可信冻结文件。"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
from camera_logs.logs import search_candidates
from mongomock_motor import AsyncMongoMockClient


class _Response:
    """最小远端 HTTP 响应替身，覆盖成功、状态异常和畸形正文。"""

    def __init__(self, body=None, error=None):
        self.body, self.error = body, error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class _Client:
    """记录内部 RPC 请求，避免测试访问真实节点。"""

    def __init__(self, response):
        self.response, self.calls = response, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


async def _repo(tmp_path, files, nodes=()):
    """建立只含候选目录的仓库替身。"""
    database = AsyncMongoMockClient().db
    await database.files.insert_many(files)
    if nodes:
        await database.nodes.insert_many(nodes)
    return SimpleNamespace(
        db=database,
        settings=SimpleNamespace(log_root=tmp_path, node_id="local", internal_token="token"),
    )


def _file(identifier, *, node="local", bytes=8, member=None):
    """生成最小 READY 冻结/当前目录项。"""
    return {"id": identifier, "status": "READY", "nodeId": node, "bytes": bytes,
            "archiveMember": member or f"{identifier}.log", "path": f"{identifier}.tar.gz"}


async def test_local_cold_index_keeps_all_and_warm_negative_index_removes_all(tmp_path, monkeypatch):
    """索引缺失不排除任何文件；可验证的本地双阴性才允许零候选。"""
    files = [_file("a"), _file("b")]
    repo = await _repo(tmp_path, files)
    monkeypatch.setattr(search_candidates, "query_index", lambda *_args: None)
    assert await search_candidates.search_candidates(repo, files, "needle") == {"a", "b"}

    def negative(_path, _keyword):
        return {file["archiveMember"]: {"bytes": file["bytes"], "head": b"line\n", "tail": b"line\n",
                                        "hasNewline": True, "mayMatch": False} for file in files}

    monkeypatch.setattr(search_candidates, "query_index", negative)
    assert await search_candidates.search_candidates(repo, files, "needle") == set()


async def test_frozen_metadata_mismatch_never_uses_current_negative_evidence(tmp_path, monkeypatch):
    """当前目录已变更成员或水位时，旧冻结搜索必须按原文扫描。"""
    current = _file("a", bytes=9, member="new.log")
    frozen = _file("a", bytes=8, member="old.log")
    repo = await _repo(tmp_path, [current])
    monkeypatch.setattr(search_candidates, "query_index", lambda *_args: {})
    assert await search_candidates.search_candidates(repo, [frozen], "needle") == {"a"}


async def test_remote_negative_evidence_and_rpc_failures_are_conservative(tmp_path, monkeypatch):
    """远端成功阴性可跳过；404、超时和畸形正文必须完整回退。"""
    files = [_file("remote", node="node-2")]
    repo = await _repo(tmp_path, files, [{"id": "node-2", "url": "http://node-2"}])
    negative = {"items": [{"fileId": "remote", "bytes": 8, "headHex": "6c696e650a", "tailHex": "6c696e650a",
                             "hasNewline": True, "mayMatch": False}]}
    client = _Client(_Response(negative))
    monkeypatch.setattr(search_candidates.httpx, "AsyncClient", lambda **_kwargs: client)
    assert await search_candidates.search_candidates(repo, files, "needle") == set()
    assert client.calls[0][0][0].endswith("/internal/search-index/candidates")

    for response in (
        _Response(error=httpx.HTTPStatusError("404", request=httpx.Request("POST", "http://node-2"), response=httpx.Response(404))),
        _Response(httpx.ReadTimeout("timeout")),
        _Response({"items": [{"fileId": "remote", "headHex": "bad"}]}),
    ):
        monkeypatch.setattr(search_candidates.httpx, "AsyncClient",
                            lambda _response=response, **_kwargs: _Client(_response))
        assert await search_candidates.search_candidates(repo, files, "needle") == {"remote"}
