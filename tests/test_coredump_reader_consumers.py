"""coredump 快照消费者的读者租约回收和取消时序回归。"""

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from camera_logs.coredumps import jobs
from camera_logs.coredumps.snapshot_lifecycle import reconcile_snapshots, release_snapshot
from camera_logs.coredumps.snapshot_readers import SnapshotReader
from camera_logs.coredumps.snapshots import freeze
from camera_logs.logs.file_reads import FileReads
from camera_logs.node.files import _limited_response, install_node_routes
from fastapi import FastAPI
from starlette.requests import Request
from test_coredump_snapshot_races import _catalog
from test_coredump_storage import repository


def request() -> Request:
    """构造最小内部下载请求，不启动节点 HTTP 服务。"""
    return Request({"type": "http", "method": "GET", "path": "/internal/coredumps/content", "headers": []})


async def test_limited_response_closes_reader_once_after_body_complete(tmp_path):
    """正文正常读完后，fd、下载名额和快照 reader 都只各回收一次。"""
    source = tmp_path / "frozen.core"
    source.write_bytes(b"content")
    reads, closed = FileReads(), []

    async def on_close():
        closed.append("closed")

    response = await _limited_response(
        reads, asyncio.Semaphore(1), source, request(), filename="core", on_close=on_close,
    )
    assert b"".join([part async for part in response.body_iterator]) == b"content"
    assert closed == ["closed"]
    await reads.close()


async def test_limited_response_closes_reader_when_response_start_fails(tmp_path):
    """ASGI 尚未开始正文即失败时也必须归还固定副本读者 lease。"""
    source = tmp_path / "frozen.core"
    source.write_bytes(b"content")
    reads, closed = FileReads(), []

    async def on_close():
        closed.append("closed")

    response = await _limited_response(
        reads, asyncio.Semaphore(1), source, request(), filename="core", on_close=on_close,
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            raise RuntimeError("response start failed")

    with pytest.raises(RuntimeError, match="response start failed"):
        await response({"type": "http", "asgi": {"version": "3.0"}, "method": "GET", "path": "/"}, receive, send)
    assert closed == ["closed"]
    await reads.close()


async def test_export_keeps_reader_until_cancelled_fetch_exits(tmp_path, monkeypatch):
    """取消长抓取时，reader 要等抓取协程实际退出后才关闭，线程收尾规则不被绕过。"""
    repo = await repository(tmp_path, quota=200_000)
    source = {"id": "file", "nodeId": "node-a", "resourceId": "camera", "version": 1, "source": {"size": 1}}
    await repo.db.coredump_files.insert_one(source | {"name": "core", "size": 1})
    job = {"id": "export", "actor": "verify", "status": "RUNNING", "workerInstanceId": "worker",
           "estimatedBytes": 1, "sources": [source]}
    await repo.db.coredump_exports.insert_one(job)
    events, started, release = [], asyncio.Event(), asyncio.Event()

    class Reader:
        """记录异步上下文退出时机的最小 reader 替身。"""

        def __init__(self, _repo, document):
            self.document = document

        async def __aenter__(self):
            events.append("reader-acquired")
            return self

        async def __aexit__(self, *_args):
            events.append("reader-closed")

        def assert_active(self):
            return None

    async def freeze(_repo, file):
        events.append("frozen")
        return file | {"status": "FROZEN", "snapshot": {"reservationToken": "token"}}

    async def fetch(_repo, _file, _target, _limit, reader):
        assert reader is not None
        events.append("fetch-started")
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            events.append("fetch-cancelled")
            await release.wait()
            events.append("fetch-finished")
            raise

    monkeypatch.setattr(jobs, "SnapshotReader", Reader)
    monkeypatch.setattr(jobs, "_freeze", freeze)
    monkeypatch.setattr(jobs, "_fetch", fetch)
    task = asyncio.create_task(jobs.run_export(repo, job))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert events == ["frozen", "reader-acquired", "fetch-started", "fetch-cancelled"]
    release.set()
    assert (await task)["status"] == "CANCELLED"
    assert events[-2:] == ["fetch-finished", "reader-closed"]


async def test_remote_fetch_forwards_parent_reader_identity(tmp_path, monkeypatch):
    """协调节点 reader 的身份必须随内部请求送达退休快照所在节点。"""
    received_headers = []

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        async def aiter_bytes(self, _size):
            yield b"ok"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, _method, _url, *, headers):
            received_headers.append(headers)
            return Response()

    class Reader:
        identifier = "parent-reader"

        def assert_active(self):
            return None

    class Repo:
        settings = SimpleNamespace(node_id="coordinator", internal_token="secret")

        async def get(self, _collection, _identifier):
            return {"url": "http://node"}

    monkeypatch.setattr(jobs.httpx, "AsyncClient", lambda **_kwargs: Client())
    digest = hashlib.sha256(b"ok").hexdigest()
    result = await jobs._fetch(
        Repo(),
        {"id": "core", "nodeId": "source-node", "snapshot": {"size": 2, "etag": f'"{digest}"'}},
        tmp_path / "stage.core",
        10,
        Reader(),
    )
    assert result.read_bytes() == b"ok"
    assert received_headers == [{"Authorization": "Bearer secret", "X-Coredump-Reader": "parent-reader"}]


async def test_retiring_node_content_allows_only_valid_parent_reader_and_releases_child(tmp_path):
    """真实节点路由仅让有效父 reader 完成退休副本的 Range 下载，并在流结束后清理 child。"""
    import httpx

    repo = await repository(tmp_path)
    repo.settings.internal_token = "internal-test-token"
    source_path = tmp_path / "nfs" / "192.0.2.120" / "core.bin"
    entry = await _catalog(repo, "192.0.2.120", "core.bin", b"retiring snapshot payload")
    frozen = await freeze(repo, entry)
    snapshot_path = Path(frozen["snapshot"]["path"])
    parent = await SnapshotReader(repo, frozen).acquire()
    assert await release_snapshot(repo, frozen)
    app = FastAPI()
    reads = install_node_routes(app, repo, SimpleNamespace(log_root=repo.settings.log_root, active={}))
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer internal-test-token"}
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://node") as client:
            missing = await client.get(f"/internal/coredumps/{entry['id']}/content", headers=headers)
            invalid = await client.get(
                f"/internal/coredumps/{entry['id']}/content",
                headers=headers | {"X-Coredump-Reader": "invalid-reader"},
            )
            response = await client.get(
                f"/internal/coredumps/{entry['id']}/content",
                headers=headers | {"X-Coredump-Reader": parent.identifier, "Range": "bytes=0-"},
            )
        assert missing.status_code == 409
        assert invalid.status_code == 404
        assert response.status_code == 206
        assert response.headers["etag"] == frozen["snapshot"]["etag"]
        assert hashlib.sha256(response.content).hexdigest() == frozen["snapshot"]["sha256"]
        current = await repo.db.coredump_files.find_one({"id": entry["id"]})
        assert [item["id"] for item in current["snapshotReaders"]] == [parent.identifier]
        await parent.close()
        assert await reconcile_snapshots(repo) == 1
        current = await repo.db.coredump_files.find_one({"id": entry["id"]})
        assert current["status"] == "RECEIVING"
        assert not snapshot_path.exists()
        assert source_path.read_bytes() == b"retiring snapshot payload"
    finally:
        await reads.close()
