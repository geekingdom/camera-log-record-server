"""维护回归：默认保留期、下载保护及归档发布后目录登记中断的恢复。"""

import hashlib
import io
import json
import tarfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.logs.maintenance import _archive_id, apply_retention, recover_orphan_archives
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


def repository(root):
    """隔离文件系统和数据库，避免维护测试访问真实采集目录。"""
    return Repository(AsyncMongoMockClient().db, Settings(
        log_root=root, node_id="node", retention_days=7,
        encryption_key=Fernet.generate_key().decode()))


def archive_at(root, *, wrong_digest=False):
    """构造带正文及完整性清单的已发布归档，模拟进程在数据库回写前退出。"""
    path = root / "task" / "run" / "session" / "part-001.tar.gz"
    path.parent.mkdir(parents=True)
    raw = b"ordered device output\n"
    manifest = {"taskId": "task", "runId": "run", "sessionId": "session",
        "hourStart": now().replace(minute=0, second=0, microsecond=0).isoformat(),
        "rawSize": len(raw), "sha256": "invalid" if wrong_digest else hashlib.sha256(raw).hexdigest(),
        "firstSequence": 1, "lastSequence": 1}
    with tarfile.open(path, "w:gz") as bundle:
        for name, data in (("part-001.log", raw), ("manifest.json", json.dumps(manifest).encode())):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return path, manifest


async def test_retention_removes_unprotected_archive_without_retain_until(tmp_path):
    repo = repository(tmp_path)
    path = tmp_path / "old.tar.gz"
    path.write_bytes(b"published")
    await repo.db.files.insert_one({"id": "old", "nodeId": "node", "status": "READY",
        "hour": (now() - timedelta(days=8)).isoformat(), "path": str(path)})
    result = await apply_retention(repo)
    assert result["removed"] == 1
    assert not path.exists()
    assert await repo.db.files.find_one({"id": "old"}) is None


async def test_retention_preserves_recent_protected_remote_and_job_references(tmp_path):
    repo = repository(tmp_path)
    for identifier, fields in (
        ("recent", {"hour": now().isoformat()}),
        ("protected", {"retainUntil": now() + timedelta(hours=1)}),
        ("remote", {"nodeId": "other-node"}),
        ("job", {}),
    ):
        path = tmp_path / f"{identifier}.tar.gz"
        path.write_bytes(b"published")
        await repo.db.files.insert_one({"id": identifier, "nodeId": "node", "status": "READY",
            "hour": (now() - timedelta(days=8)).isoformat(), "path": str(path), **fields})
    await repo.db.jobs.insert_one({"status": "QUEUED", "files": [{"id": "job"}]})
    result = await apply_retention(repo)
    assert result["removed"] == 0
    assert len(list(tmp_path.glob("*.tar.gz"))) == 4


async def test_recovery_repairs_open_record_after_archive_publication(tmp_path):
    repo = repository(tmp_path)
    path, manifest = archive_at(tmp_path)
    identifier = _archive_id(tmp_path, path)
    await repo.db.files.insert_one({"id": identifier, "status": "OPEN", "nodeId": "node",
        "taskId": "task", "runId": "run", "sessionId": "session",
        "path": str(path.with_suffix("").with_suffix(".log")), "bytes": 1})
    assert await recover_orphan_archives(repo) == 1
    record = await repo.db.files.find_one({"id": identifier})
    assert record["status"] == "READY"
    assert record["bytes"] == manifest["rawSize"]
    assert Path(record["path"]) == path
    assert record["archiveBytes"] == path.stat().st_size
    assert await recover_orphan_archives(repo) == 0


async def test_recovery_rejects_checksum_mismatch_and_preserves_archive(tmp_path):
    repo = repository(tmp_path)
    path, _ = archive_at(tmp_path, wrong_digest=True)
    assert await recover_orphan_archives(repo) == 0
    assert await repo.db.files.count_documents({}) == 0
    assert path.exists()


async def test_recovery_does_not_resurrect_retention_claim(tmp_path):
    repo = repository(tmp_path)
    path, _ = archive_at(tmp_path)
    await repo.db.files.insert_one({"id": _archive_id(tmp_path, path), "status": "DELETING"})
    assert await recover_orphan_archives(repo) == 0
    assert (await repo.db.files.find_one({}))["status"] == "DELETING"


async def test_recovery_preserves_mismatched_session_identity(tmp_path):
    repo = repository(tmp_path)
    path, _ = archive_at(tmp_path)
    await repo.db.files.insert_one({"id": _archive_id(tmp_path, path), "status": "OPEN",
        "nodeId": "node", "taskId": "task", "runId": "run", "sessionId": "other"})
    assert await recover_orphan_archives(repo) == 0
    assert (await repo.db.files.find_one({}))["sessionId"] == "other"


async def test_retention_respects_protection_added_after_candidate_scan(tmp_path):
    repo = repository(tmp_path)
    repo.db = SimpleNamespace(files=repo.db.files, jobs=repo.db.jobs)
    path = tmp_path / "old.tar.gz"
    path.write_bytes(b"published")
    await repo.db.files.insert_one({"id": "old", "nodeId": "node", "status": "READY",
        "hour": (now() - timedelta(days=8)).isoformat(), "path": str(path)})

    async def protect_before_claim(*args, **kwargs):
        await repo.db.files.update_one({"id": "old"},
            {"$set": {"retainUntil": now() + timedelta(hours=1)}})

    repo.db.jobs.find_one = AsyncMock(side_effect=protect_before_claim)
    assert (await apply_retention(repo))["removed"] == 0
    assert path.exists()


async def test_retention_finishes_interrupted_deletions_only_on_local_node(tmp_path):
    repo = repository(tmp_path)
    for identifier, node in (("present", "node"), ("missing", "node"), ("remote", "other")):
        path = tmp_path / f"{identifier}.tar.gz"
        if identifier != "missing":
            path.write_bytes(b"published")
        await repo.db.files.insert_one({"id": identifier, "nodeId": node,
            "status": "DELETING", "path": str(path)})
    assert (await apply_retention(repo))["removed"] == 2
    assert await repo.db.files.count_documents({}) == 1
    assert (await repo.db.files.find_one({}))["id"] == "remote"
    assert not (tmp_path / "present.tar.gz").exists()
    assert (tmp_path / "remote.tar.gz").exists()
