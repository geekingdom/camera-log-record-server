"""内部文件接口回归：权限、节点归属、固定水位和已归档字节读取。"""
import asyncio
import base64
import tarfile

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.files import install_node_routes
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient


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
