"""原始分卷删除与目录发布之间，读取和快照必须保持同一文件及冻结水位。"""

import asyncio
import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs.archive_access import snapshot
from camera_logs.logs.compression import publish_hour_archive
from camera_logs.logs.source import open_log_source
from camera_logs.node.files import install_node_routes
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient


def published_hour(tmp_path):
    """发布含两段正文的真实小时包，返回已删除的第二段原始路径。"""
    segments = []
    for number, data in enumerate((b"wrong-file", b"abcdefUNCONFIRMED"), 1):
        log = tmp_path / f"renamed-task-part-{number:06d}.log"
        index = log.with_suffix(".index.jsonl")
        log.write_bytes(data)
        index.write_bytes(b"{}\n")
        segments.append({"logPath": str(log), "logName": log.name, "indexName": index.name,
                         "sha256": hashlib.sha256(data).hexdigest(), "rawSize": len(data),
                         "indexSha256": hashlib.sha256(index.read_bytes()).hexdigest(), "indexBytes": 3})
    # 修改任务名称后，小时包可能沿用旧名称，不能只替换原文件名后缀来定位。
    publish_hour_archive(segments, tmp_path / "original-task-hour.tar.gz")
    return Path(segments[1]["logPath"])


def stale_catalog_app(tmp_path, path):
    """模拟磁盘已归档、数据库仍登记 OPEN 原路径的合法过渡状态。"""
    settings = Settings(encryption_key=Fernet.generate_key().decode(), internal_token="node-secret",
                        log_root=tmp_path, node_id="node")
    repo = Repository(AsyncMongoMockClient().db, settings)
    asyncio.run(repo.db.files.insert_one({"id": "file", "taskId": "task", "runId": "run",
        "sessionId": "session", "nodeId": "node", "status": "OPEN", "path": str(path), "bytes": 6}))
    app = FastAPI()
    install_node_routes(app, repo, SimpleNamespace(log_root=tmp_path, active={}))
    return app


@pytest.mark.parametrize("endpoint", ["read/file?offset=2&limit=3", "archive/file?bytes=3"])
def test_stale_open_catalog_reads_exact_archived_member(tmp_path, endpoint):
    """归档回退不能读到同包第一段或超出请求前冻结的字节范围。"""
    path = published_hour(tmp_path)
    assert not path.exists()
    with TestClient(stale_catalog_app(tmp_path, path)) as client:
        response = client.get(f"/internal/{endpoint}", headers={"Authorization": "Bearer node-secret"})
    assert response.status_code == 200
    if endpoint.startswith("read"):
        assert response.json()["fileId"] == "file"
        assert response.json()["sessionId"] == "session"
        assert response.json()["nextOffset"] == 5
        assert base64.b64decode(response.json()["data"]) == b"cde"
    else:
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as bundle:
            assert bundle.extractfile(path.name).read() == b"abc"


def test_local_job_snapshot_survives_deleted_raw_file(tmp_path):
    """本地搜索和导出使用的快照入口同样覆盖目录滞后，不依赖 HTTP 重试。"""
    path = published_hour(tmp_path)
    target = tmp_path / "exports" / "snapshot.tar.gz"
    snapshot(path, target, 3, path.with_suffix(".index.jsonl"), include_index=True)
    with tarfile.open(target, "r:gz") as bundle:
        assert bundle.extractfile(path.name).read() == b"abc"
        assert bundle.extractfile(path.with_suffix(".index.jsonl").name).read() == b"{}\n"


@pytest.mark.parametrize("offset", [6, 100])
def test_archived_eof_does_not_scan_body_or_spend_read_budget(tmp_path, monkeypatch, offset):
    """已达冻结水位的续读只验证成员身份，不能反复扫描已读正文占用共享带宽。"""
    path = published_hour(tmp_path)
    consumed = []
    monkeypatch.setattr("camera_logs.logs.archive_access.read_limiter.consume", consumed.append)
    with TestClient(stale_catalog_app(tmp_path, path)) as client:
        response = client.get(f"/internal/read/file?offset={offset}",
                              headers={"Authorization": "Bearer node-secret"})
    assert response.status_code == 200
    assert response.json()["data"] == ""
    assert response.json()["nextOffset"] == offset
    assert sum(consumed) == 0


def test_raw_open_pins_inode_across_unlink(tmp_path):
    """原文件已打开后被删除，当前请求仍能读完原 inode，退出后句柄关闭。"""
    path = tmp_path / "raw.log"
    path.write_bytes(b"original")
    with open_log_source(path) as source:
        path.unlink()
        assert source.size == 8
        assert source.stream.read() == b"original"
    assert source.stream.closed


def test_raw_disappears_at_open_boundary(tmp_path, monkeypatch):
    """即使调用前原路径仍存在，也必须覆盖工作线程打开时恰好被归档删除的竞争。"""
    path = published_hour(tmp_path)
    path.write_bytes(b"abcdefUNCONFIRMED")
    original_open = Path.open

    def remove_before_open(self, *args, **kwargs):
        if self == path:
            self.unlink(missing_ok=True)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", remove_before_open)
    with open_log_source(path) as source:
        assert source.stream.read() == b"abcdefUNCONFIRMED"
    assert source.stream.closed
    assert source.archive.closed


@pytest.mark.parametrize("damage", ["missing_member", "wrong_size", "no_manifest", "outside_archive", "outside_manifest"])
def test_archive_fallback_rejects_unproven_or_outside_source(tmp_path, damage):
    """回退只接受同目录正式清单与精确成员，不猜测正文或读取目录外的目标。"""
    directory = tmp_path / "hour"
    directory.mkdir()
    path = published_hour(directory)
    archive = directory / "original-task-hour.tar.gz"
    metadata = archive.with_name(archive.name + ".metadata.json")
    if damage in {"missing_member", "wrong_size"}:
        data = json.loads(metadata.read_text())
        if damage == "missing_member":
            data["members"][1]["logName"] = "other.log"
        else:
            data["members"][1]["rawSize"] += 1
        metadata.write_text(json.dumps(data))
    elif damage == "no_manifest":
        metadata.unlink()
    else:
        selected = archive if damage == "outside_archive" else metadata
        outside = tmp_path / selected.name
        selected.rename(outside)
        selected.symlink_to(outside)
    with pytest.raises((FileNotFoundError, OSError, ValueError)), open_log_source(path):
        pytest.fail("不应打开未证明归属的归档")
