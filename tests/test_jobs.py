"""后台作业回归：冻结水位、字面搜索、结果上限、严格缺片与取消状态竞争。"""
import asyncio
import json
import tarfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs import jobs
from camera_logs.logs.archive_access import snapshot
from camera_logs.logs.jobs import run_job
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


def test_snapshot_uses_frozen_byte_length_and_records_checksum(tmp_path):
    source, target = tmp_path / "live.log", tmp_path / "fixed.tar.gz"
    source.write_bytes(b"first\nsecond\n")
    snapshot(source, target, len(b"first\n"), file_id="frozen")
    source.write_bytes(b"first\nsecond\nthird\n")
    with tarfile.open(target, "r:gz") as archive:
        raw = archive.extractfile("live.log").read()
        manifest = json.loads(archive.extractfile("manifest.json").read())
    assert raw == b"first\n"
    assert manifest["rawBytes"] == len(raw)
    assert manifest["sha256"]


def test_download_snapshots_open_file_and_searches_literal(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        source = tmp_path / "logs" / "part.log"
        source.parent.mkdir()
        source.write_bytes(b"before\nneedle value\nafter\n")
        file = {"id": "f1", "taskId": "task", "runId": "run", "sessionId": "session", "hour": "2026-09-08T00:00:00+00:00", "path": str(source), "status": "OPEN", "bytes": source.stat().st_size, "nodeId": "node"}
        await repo.db.files.insert_one(file)
        async def audit(*_): pass
        repo.audit = audit
        download = {"id": "download", "kind": "DOWNLOAD", "status": "RUNNING", "actor": "u", "files": [{"id": "f1", "nodeId": "node", "status": "OPEN", "bytes": source.stat().st_size}]}
        await repo.db.jobs.insert_one(download)
        outcome = await run_job(repo, download)
        assert outcome["status"] == "SUCCEEDED"
        assert Path(outcome["resultPath"]).is_file()
        assert outcome["filename"] == "part.tar.gz"
        search = {"id": "search", "kind": "SEARCH", "status": "RUNNING", "actor": "u", "keyword": "needle", "start": "2026-09-07T00:00:00+00:00", "end": "2026-09-09T00:00:00+00:00", "files": [{"id": "f1", "nodeId": "node", "status": "OPEN", "bytes": source.stat().st_size}]}
        await repo.db.jobs.insert_one(search)
        found = await run_job(repo, search)
        assert found["status"] == "SUCCEEDED"
        assert found["results"][0]["fileId"] == "f1"
        assert "needle" in found["results"][0]["text"]
    asyncio.run(scenario())


def test_remote_snapshot_uses_archive_metadata_name_and_old_records_fall_back(tmp_path, monkeypatch):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="local")
        repo = Repository(AsyncMongoMockClient().db, settings)
        await repo.db.files.insert_one({
            "id": "remote", "nodeId": "remote-node", "status": "OPEN", "bytes": 3,
            "archiveName": "任务/10.0.0.1-20260908090000-part-001.tar.gz",
        })

        async def remote_copy(_repo, _file, _frozen, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"copy")
            return target

        monkeypatch.setattr(jobs, "_remote_archive", remote_copy)
        path, temporary = await jobs._archive(
            repo, {"id": "remote", "status": "OPEN", "bytes": 3}, tmp_path / "scratch"
        )
        old_name = jobs._archive_name({}, "old-id", use_path=False)
        remote_old_name = jobs._archive_name({"path": "/remote/readable.log"}, "remote-old", use_path=False)
        return path.name, temporary, old_name, remote_old_name

    name, temporary, old_name, remote_old_name = asyncio.run(scenario())
    assert name == "任务-10.0.0.1-20260908090000-part-001.tar.gz"
    assert temporary is True
    assert old_name == "old-id.tar.gz"
    assert remote_old_name == "remote-old.tar.gz"


def test_node_temp_reservation_counts_existing_tmp_files(tmp_path, monkeypatch):
    async def scenario():
        repo = SimpleNamespace(settings=SimpleNamespace(log_root=tmp_path))
        occupied = tmp_path / "exports" / ".tmp" / "other" / "archive.tar.gz"
        occupied.parent.mkdir(parents=True)
        occupied.write_bytes(b"123456")
        monkeypatch.setattr(jobs, "TEMP_LIMIT", 10)
        with pytest.raises(ValueError, match="temporary export storage"):
            await jobs._reserve_temp(repo, "new-job", 5)

    asyncio.run(scenario())


def test_single_archive_download_preserves_readable_archive_filename(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        archive = tmp_path / "站点 A-10.0.0.1-20260908090000-20260908100000-run-sess-part-001.tar.gz"
        archive.write_bytes(b"archive")
        await repo.db.files.insert_one({"id": "f1", "path": str(archive), "status": "READY", "bytes": 7, "nodeId": "node"})
        async def audit(*_):
            return None

        repo.audit = audit
        job = {"id": "download", "kind": "DOWNLOAD", "status": "RUNNING", "files": [{"id": "f1", "nodeId": "node", "status": "READY", "bytes": 7}]}
        await repo.db.jobs.insert_one(job)
        return await run_job(repo, job), archive.name

    result, expected_name = asyncio.run(scenario())
    assert result["filename"] == expected_name
    assert Path(result["resultPath"]).name == expected_name


def test_zip_export_keeps_readable_names_and_disambiguates_duplicates(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        first, second = tmp_path / "one" / "same.tar.gz", tmp_path / "two" / "same.tar.gz"
        first.parent.mkdir(); second.parent.mkdir()
        first.write_bytes(b"one"); second.write_bytes(b"two")
        for identifier, path in (("f1", first), ("f2", second)):
            await repo.db.files.insert_one({"id": identifier, "path": str(path), "status": "READY", "bytes": 3, "nodeId": "node"})
        async def audit(*_):
            return None

        repo.audit = audit
        job = {"id": "download", "kind": "DOWNLOAD", "status": "RUNNING", "files": [
            {"id": "f1", "nodeId": "node", "status": "READY", "bytes": 3},
            {"id": "f2", "nodeId": "node", "status": "READY", "bytes": 3},
        ]}
        await repo.db.jobs.insert_one(job)
        result = await run_job(repo, job)
        with zipfile.ZipFile(result["resultPath"]) as bundle:
            return bundle.namelist()

    names = asyncio.run(scenario())
    assert names[:2] == ["same.tar.gz", "same-2.tar.gz"]


def test_search_caps_results_at_one_thousand(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        source = tmp_path / "many.log"
        source.write_bytes(b"needle\n" * 1100)
        await repo.db.files.insert_one({"id": "many", "path": str(source), "status": "OPEN", "bytes": source.stat().st_size, "nodeId": "node"})
        async def audit(*_): pass
        repo.audit = audit
        job = {"id": "many-search", "kind": "SEARCH", "status": "RUNNING", "files": [{"id": "many", "nodeId": "node", "status": "OPEN", "bytes": source.stat().st_size}], "keyword": "needle", "start": "2026-09-07T00:00:00+00:00", "end": "2026-09-09T00:00:00+00:00"}
        await repo.db.jobs.insert_one(job)
        result = await run_job(repo, job)
        assert len(result["results"]) == 1000
        assert result["truncated"] is True
    asyncio.run(scenario())


def test_cancelled_job_is_never_replaced_by_success(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        job = {"id": "cancelled", "kind": "DOWNLOAD", "status": "CANCELLED", "files": []}
        await repo.db.jobs.insert_one(job)
        result = await run_job(repo, job)
        assert result == {"status": "CANCELLED"}
        assert (await repo.db.jobs.find_one({"id": "cancelled"}))["status"] == "CANCELLED"
    asyncio.run(scenario())


def test_partial_export_fails_when_partial_results_are_not_allowed(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        path = tmp_path / "ok.log"; path.write_bytes(b"ok")
        await repo.db.files.insert_one({"id": "ok", "path": str(path), "status": "OPEN", "bytes": 2, "nodeId": "node"})
        async def audit(*_): pass
        repo.audit = audit
        job = {"id": "partial", "kind": "DOWNLOAD", "status": "RUNNING", "allowPartial": False, "files": [{"id": "ok", "status": "OPEN", "bytes": 2}, {"id": "missing", "status": "OPEN", "bytes": 1}]}
        await repo.db.jobs.insert_one(job)
        assert (await run_job(repo, job))["status"] == "FAILED"
        assert not (tmp_path / "exports" / "partial").exists()
    asyncio.run(scenario())


def test_concurrent_cancellation_cleans_completed_output(tmp_path, monkeypatch):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        job = {"id": "race", "kind": "DOWNLOAD", "status": "RUNNING", "files": []}
        await repo.db.jobs.insert_one(job)
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_download(_repo, _job):
            output = tmp_path / "exports" / "race"; output.mkdir(parents=True); (output / "race.tar.gz").write_bytes(b"done")
            started.set(); await release.wait()
            return {"resultPath": str(output / "race.tar.gz"), "filename": "race.tar.gz", "missing": [], "bytes": 4}
        monkeypatch.setattr(jobs, "_download", slow_download)
        task = asyncio.create_task(run_job(repo, job)); await started.wait()
        await repo.db.jobs.update_one({"id": "race"}, {"$set": {"status": "CANCELLED"}}); release.set()
        assert (await task)["status"] == "CANCELLED"
        assert not (tmp_path / "exports" / "race").exists()
    asyncio.run(scenario())


def test_expired_running_job_does_not_start_work(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        job = {"id": "expired", "kind": "DOWNLOAD", "status": "RUNNING", "expiresAt": datetime.now(UTC) - timedelta(seconds=1), "files": []}
        await repo.db.jobs.insert_one(job)
        assert (await run_job(repo, job))["status"] == "EXPIRED"
        assert (await repo.db.jobs.find_one({"id": "expired"}))["status"] == "EXPIRED"
    asyncio.run(scenario())
