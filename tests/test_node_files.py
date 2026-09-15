"""内部文件接口回归：权限、节点归属、固定水位和已归档字节读取。"""
import asyncio
import base64
import tarfile
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.files import install_node_routes
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient
from starlette.requests import Request


def live_app(tmp_path, *, task_id="task", run_id="run", session_id="session", status="OPEN"):
    """磁盘包含未确认尾部，目录与活跃会话水位分别落后，用于验证读边界。"""
    settings = Settings(encryption_key=Fernet.generate_key().decode(), internal_token="node-secret",
                        log_root=tmp_path, node_id="node")
    repo = Repository(AsyncMongoMockClient().db, settings)
    path = tmp_path / "live.log"
    path.write_bytes(b"abcdefUNCONFIRMED")
    asyncio.run(repo.db.files.insert_one({"id": "live", "taskId": "task", "runId": "run",
        "sessionId": "session", "nodeId": "node", "status": status, "path": str(path), "bytes": 3}))
    session = SimpleNamespace(task={"id": task_id, "runId": run_id},
                              collector=SimpleNamespace(session_id=session_id), paths={"live": 6}, retired=False)
    app = FastAPI()
    install_node_routes(app, repo, SimpleNamespace(log_root=tmp_path, active={"task": session}))
    return app


def test_live_read_uses_confirmed_session_watermark_without_waiting_for_catalog(tmp_path):
    """秒级目录落后时可以立即读取已确认的 def，但不能暴露磁盘未确认尾部。"""
    with TestClient(live_app(tmp_path)) as client:
        result = client.get("/internal/read/live?offset=3", headers={"Authorization": "Bearer node-secret"})
    assert result.status_code == 200
    assert base64.b64decode(result.json()["data"]) == b"def"
    assert result.json()["nextOffset"] == 6


@pytest.mark.parametrize("changed", [
    {"task_id": "other"}, {"run_id": "other"}, {"session_id": "other"}, {"status": "READY"},
])
def test_live_read_cannot_use_another_identity_or_override_sealed_watermark(tmp_path, changed):
    """新会话及其他运行的内存水位不能扩大旧文件或已封存文件的读取范围。"""
    with TestClient(live_app(tmp_path, **changed)) as client:
        result = client.get("/internal/read/live?offset=3", headers={"Authorization": "Bearer node-secret"})
    assert result.status_code == 200
    assert base64.b64decode(result.json()["data"]) == b""


def test_current_hour_snapshot_keeps_explicit_frozen_watermark(tmp_path):
    """实时读取水位扩大不能改变已请求的固定三字节下载快照。"""
    import io
    with TestClient(live_app(tmp_path)) as client:
        result = client.get("/internal/archive/live?bytes=3", headers={"Authorization": "Bearer node-secret"})
    assert result.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(result.content), mode="r:gz") as archive:
        assert archive.extractfile("live.log").read() == b"abc"


def test_internal_read_requires_token_and_returns_bounded_base64(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), internal_token="node-secret", log_root=tmp_path)
        repo = Repository(AsyncMongoMockClient().db, settings)
        path = tmp_path / "source.log"
        path.write_bytes(b"abcdef")
        await repo.db.files.insert_one({"id": "f1", "path": str(path), "status": "OPEN", "sessionId": "s"})
        return repo

    repo = asyncio.run(scenario())
    app = FastAPI()
    install_node_routes(app, repo, type("Runtime", (), {"log_root": tmp_path})())
    with TestClient(app) as client:
        assert client.get("/internal/read/f1").status_code == 401
        response = client.get("/internal/read/f1?offset=2&limit=3", headers={"Authorization": "Bearer node-secret"})
    assert response.status_code == 200
    assert base64.b64decode(response.json()["data"]) == b"cde"
    assert response.json()["nextOffset"] == 5


def test_internal_read_reads_ready_archive_raw_member(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), internal_token="node-secret", log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        archive = tmp_path / "ready.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            import io
            info = tarfile.TarInfo("part.log"); info.size = 6; output.addfile(info, io.BytesIO(b"abcdef"))
        await repo.db.files.insert_one({"id": "ready", "path": str(archive), "status": "READY", "bytes": 6, "nodeId": "node"})
        return repo
    repo = asyncio.run(scenario()); app = FastAPI(); install_node_routes(app, repo, type("Runtime", (), {"log_root": tmp_path})())
    with TestClient(app) as client:
        response = client.get("/internal/read/ready?offset=2&limit=3", headers={"Authorization": "Bearer node-secret"})
    assert base64.b64decode(response.json()["data"]) == b"cde"


def test_internal_read_uses_exact_shared_archive_member(tmp_path):
    """共享小时包必须按文件记录的成员名读取，不能回退到包内第一段。"""
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), internal_token="node-secret", log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        archive = tmp_path / "shared.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            import io
            for name, data in (("part-000001.log", b"first"), ("part-000002.log", b"second")):
                info = tarfile.TarInfo(name); info.size = len(data); output.addfile(info, io.BytesIO(data))
        await repo.db.files.insert_one({"id": "second", "path": str(archive), "archiveMember": "part-000002.log", "status": "READY", "bytes": 6, "nodeId": "node"})
        await repo.db.files.insert_one({"id": "missing", "path": str(archive), "archiveMember": "part-999999.log", "status": "READY", "bytes": 1, "nodeId": "node"})
        return repo
    repo = asyncio.run(scenario()); app = FastAPI(); install_node_routes(app, repo, type("Runtime", (), {"log_root": tmp_path})())
    with TestClient(app) as client:
        response = client.get("/internal/read/second", headers={"Authorization": "Bearer node-secret"})
        missing = client.get("/internal/read/missing", headers={"Authorization": "Bearer node-secret"})
    assert base64.b64decode(response.json()["data"]) == b"second"
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_internal_download_holds_export_reader_until_body_is_consumed(tmp_path):
    """节点响应创建后到最后一个字节发送前，维护可见的读者租约不得提前消失。"""
    settings = Settings(encryption_key=Fernet.generate_key().decode(), internal_token="node-secret",
                        log_root=tmp_path, node_id="node")
    repo = Repository(AsyncMongoMockClient().db, settings)
    output = tmp_path / "exports" / "download" / "hours.tar.gz"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"complete export")
    await repo.db.jobs.insert_one({
        "id": "download", "nodeId": "node", "kind": "DOWNLOAD", "status": "SUCCEEDED",
        "resultPath": str(output), "filename": output.name, "outputExecutionState": "CLOSED",
        "outputWriterNodeId": "node", "outputWriterClosedAt": datetime.now(UTC), "outputReaders": [],
    })
    app = FastAPI()
    reads = install_node_routes(app, repo, SimpleNamespace(log_root=tmp_path))
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/internal/downloads/{identifier}")
    request = Request({"type": "http", "method": "GET", "path": "/internal/downloads/download", "headers": []})
    try:
        response = await endpoint("download", request, None)
        claimed = await repo.db.jobs.find_one({"id": "download"})
        assert len(claimed["outputReaders"]) == 1
        assert b"".join([chunk async for chunk in response.body_iterator]) == b"complete export"
        released = await repo.db.jobs.find_one({"id": "download"})
        assert released["outputReaders"] == []
    finally:
        await reads.close()
