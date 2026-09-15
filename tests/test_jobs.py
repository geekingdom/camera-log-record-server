"""后台作业回归：冻结水位、字面搜索、结果上限、严格缺片与取消状态竞争。"""
import asyncio
import io
import json
import tarfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs import hour_download, jobs
from camera_logs.logs.archive_access import snapshot
from camera_logs.logs.jobs import run_job
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


def test_snapshot_enforces_compressed_byte_limit_before_write(tmp_path):
    """gzip 头、正文及尾部都计入上限，不能先写超额数据再事后检查。"""
    source, target = tmp_path / "source.log", tmp_path / "snapshot.tar.gz"
    source.write_bytes(b"needle\n" * 100)
    with pytest.raises(ValueError, match="snapshot storage limit"):
        snapshot(source, target, source.stat().st_size, max_output_bytes=32)
    assert target.stat().st_size <= 32


async def test_remote_snapshot_checks_limit_before_writing_chunk(tmp_path, monkeypatch):
    """远端返回超额正文时，不允许把导致越界的数据块写入临时文件。"""
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * 1_048_576
            yield b"b" * 1_048_576

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Chunks()))
    monkeypatch.setattr(jobs.httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))

    async def get_node(*_args):
        return {"url": "http://remote.test"}

    repo = SimpleNamespace(get=get_node, settings=SimpleNamespace(internal_token="synthetic"))
    target = tmp_path / "remote.tar.gz"
    with pytest.raises(ValueError, match="snapshot storage limit"):
        await jobs._remote_archive(repo, {"id": "file", "nodeId": "remote"}, {"status": "READY"},
                                   target, True, 1_048_577)
    assert target.stat().st_size == 1_048_576


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


def test_snapshot_reads_external_index_only_for_internal_search_snapshot(tmp_path):
    """新共享小时包的包外索引只在内部搜索快照中复制。"""
    archive, index = tmp_path / "shared.tar.gz", tmp_path / "shared.index.jsonl"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("part-000002.log"); info.size = 6
        bundle.addfile(info, io.BytesIO(b"needle"))
    index.write_text('{"offset": 0, "length": 6, "receivedAt": "2026-09-08T00:00:00+00:00"}\n')
    public, internal = tmp_path / "public.tar.gz", tmp_path / "internal.tar.gz"
    snapshot(archive, public, 6, index, archive_member="part-000002.log")
    snapshot(archive, internal, 6, index, archive_member="part-000002.log", include_index=True)
    with tarfile.open(public, "r:gz") as bundle:
        assert all(not name.endswith(".index.jsonl") for name in bundle.getnames())
    with tarfile.open(internal, "r:gz") as bundle:
        assert index.name in bundle.getnames()
    matches = list(jobs._search_archive(
        internal, b"needle", datetime(2026, 9, 7, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC), lambda: False,
        "part-000002.log",
    ))
    assert matches and "needle" in matches[0][1]


def test_download_snapshots_open_file_and_searches_literal(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        source = tmp_path / "logs" / "part.log"
        source.parent.mkdir()
        source.write_bytes(b"before\nneedle value\nafter\n")
        file = {"id": "f1", "taskId": "task", "runId": "run", "sessionId": "session", "hour": "2026-09-08T00:00:00+00:00", "path": str(source), "status": "OPEN", "bytes": source.stat().st_size, "nodeId": "node"}
        await repo.db.files.insert_one(file)
        async def audit(*_, **_kwargs): pass
        repo.audit = audit
        download = {"id": "download", "kind": "DOWNLOAD", "status": "RUNNING", "actor": "u", "files": [{"id": "f1", "nodeId": "node", "status": "OPEN", "bytes": source.stat().st_size}]}
        await repo.db.jobs.insert_one(download)
        outcome = await run_job(repo, download)
        assert outcome["status"] == "SUCCEEDED"
        assert Path(outcome["resultPath"]).is_file()
        assert outcome["filename"] == "task-unknown-ip-20260908080000-20260908090000.tar.gz"
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


async def test_temp_admission_rejects_unreadable_inventory(tmp_path, monkeypatch):
    """统计权限错误不能被当作零字节，从而错误放行新作业。"""
    repo = SimpleNamespace(settings=SimpleNamespace(log_root=tmp_path))
    occupied = tmp_path / "exports" / ".tmp" / "other" / "archive.tar.gz"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"occupied")
    original = Path.stat

    def stat(path, *args, **kwargs):
        if path == occupied:
            raise PermissionError("synthetic unreadable inventory")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(PermissionError):
        await jobs._reserve_temp(repo, "unreadable", 1)
    assert "unreadable" not in jobs._temp_reservations


def test_single_archive_download_rebuilds_user_hour_filename(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        archive = tmp_path / "站点 A-10.0.0.1-20260908090000-20260908100000-run-sess-part-001.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            info = tarfile.TarInfo("part-000001.log"); info.size = 7; bundle.addfile(info, io.BytesIO(b"archive"))
        await repo.db.files.insert_one({"id": "f1", "path": str(archive), "status": "READY", "bytes": 7, "nodeId": "node", "hour": "2026-09-08T00:00:00+00:00"})
        async def audit(*_, **_kwargs):
            return None

        repo.audit = audit
        job = {"id": "download", "kind": "DOWNLOAD", "status": "RUNNING", "files": [{"id": "f1", "nodeId": "node", "status": "READY", "bytes": 7}]}
        await repo.db.jobs.insert_one(job)
        return await run_job(repo, job)

    result = asyncio.run(scenario())
    assert result["filename"] == "task-unknown-ip-20260908080000-20260908090000.tar.gz"
    assert Path(result["resultPath"]).name == result["filename"]


def test_zip_export_keeps_readable_names_and_disambiguates_duplicates(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        first, second = tmp_path / "one" / "same.tar.gz", tmp_path / "two" / "same.tar.gz"
        first.parent.mkdir(); second.parent.mkdir()
        for path, data in ((first, b"one"), (second, b"two")):
            with tarfile.open(path, "w:gz") as bundle:
                info = tarfile.TarInfo("part-000001.log"); info.size = len(data); bundle.addfile(info, io.BytesIO(data))
        for identifier, path in (("f1", first), ("f2", second)):
            await repo.db.files.insert_one({"id": identifier, "path": str(path), "status": "READY", "bytes": 3, "nodeId": "node", "hour": f"2026-09-08T0{1 if identifier == 'f1' else 2}:00:00+00:00"})
        async def audit(*_, **_kwargs):
            return None

        repo.audit = audit
        job = {"id": "download", "kind": "DOWNLOAD", "status": "RUNNING", "files": [
            {"id": "f1", "nodeId": "node", "status": "READY", "bytes": 3},
            {"id": "f2", "nodeId": "node", "status": "READY", "bytes": 3},
        ]}
        await repo.db.jobs.insert_one(job)
        result = await run_job(repo, job)
        with zipfile.ZipFile(result["resultPath"]) as bundle:
            names = bundle.namelist()
            contents = []
            for name in names:
                with tarfile.open(fileobj=io.BytesIO(bundle.read(name)), mode="r:gz") as hour:
                    contents.append(hour.getnames())
            return names, contents

    names, contents = asyncio.run(scenario())
    assert len(names) == 2 and all(name.endswith(".tar.gz") for name in names)
    assert contents == [["part-000001.log"], ["part-000001.log"]]
    assert "manifest.json" not in names


@pytest.mark.parametrize("replace_during_validation", [False, True])
def test_download_reuses_complete_shared_hour_archive_without_member_leakage(tmp_path, monkeypatch, replace_during_validation):
    """完整选择同一小时包时直接下载原始压缩包，成员顺序和正文保持不变。"""
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        archive = tmp_path / "shared-hour.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for name, data in (("part-000001.log", b"first"), ("part-000002.log", b"second")):
                info = tarfile.TarInfo(name); info.size = len(data)
                bundle.addfile(info, io.BytesIO(data))
        hour = "2026-09-08T00:00:00+00:00"
        for identifier, member, size, sequence in (("f1", "part-000001.log", 5, 1), ("f2", "part-000002.log", 6, 2)):
            await repo.db.files.insert_one({
                "id": identifier, "path": str(archive), "status": "READY", "bytes": size,
                "nodeId": "node", "hour": hour, "archiveGroupId": "group", "archiveMember": member,
                "firstSequence": sequence,
            })
        async def audit(*_, **_kwargs):
            return None
        repo.audit = audit
        job = {"id": "download", "kind": "DOWNLOAD", "status": "RUNNING", "files": [
            {"id": "f2", "nodeId": "node", "status": "READY", "bytes": 6, "hour": hour},
            {"id": "f1", "nodeId": "node", "status": "READY", "bytes": 5, "hour": hour},
        ]}
        await repo.db.jobs.insert_one(job)
        if replace_during_validation:
            original = jobs.reusable_hour_archive

            def replace_after_check(sources):
                """准确注入已检查成员但尚未固定下载文件的发布窗口。"""
                selected = original(sources)
                replacement = archive.with_suffix(".new.tar.gz")
                with tarfile.open(replacement, "w:gz") as bundle:
                    info = tarfile.TarInfo("unexpected.log")
                    info.size = 3
                    bundle.addfile(info, io.BytesIO(b"new"))
                replacement.replace(archive)
                return selected

            monkeypatch.setattr(jobs, "reusable_hour_archive", replace_after_check)
        return await run_job(repo, job), archive

    result, archive = asyncio.run(scenario())
    assert Path(result["resultPath"]) != archive
    assert Path(result["resultPath"]).parent.name == "download"
    with tarfile.open(result["resultPath"], "r:gz") as bundle:
        assert bundle.getnames() == ["part-000001.log", "part-000002.log"]
        assert bundle.extractfile("part-000001.log").read() == b"first"
        assert bundle.extractfile("part-000002.log").read() == b"second"
    replacement = archive.with_suffix(".replacement.tar.gz")
    with tarfile.open(replacement, "w:gz") as bundle:
        info = tarfile.TarInfo("part-000001.log"); info.size = 7
        bundle.addfile(info, io.BytesIO(b"changed"))
    replacement.replace(archive)
    with tarfile.open(result["resultPath"], "r:gz") as bundle:
        assert bundle.extractfile("part-000001.log").read() == b"first"


def test_hour_export_splits_legacy_large_member_at_format_limit(tmp_path, monkeypatch):
    """旧档大于当前分卷限制时，导出包仍使用连续的受限分卷。"""
    source, output = tmp_path / "legacy.tar.gz", tmp_path / "hour.tar.gz"
    with tarfile.open(source, "w:gz") as bundle:
        info = tarfile.TarInfo("legacy.log"); info.size = 7
        bundle.addfile(info, io.BytesIO(b"1234567"))
    monkeypatch.setattr(hour_download, "MAX_MEMBER_BYTES", 3)
    hour_download.write_hour_archive(output, [
        ({"id": "f1", "bytes": 7}, {"id": "f1"}, source, False),
    ])
    with tarfile.open(output, "r:gz") as bundle:
        assert bundle.getnames() == ["part-000001.log", "part-000002.log", "part-000003.log"]
        assert [bundle.extractfile(name).read() for name in bundle.getnames()] == [b"123", b"456", b"7"]


def test_pin_cross_filesystem_copies_only_frozen_member(tmp_path, monkeypatch):
    """硬链接不可用时仅复制所选水位，不复制同包的其他大成员。"""
    source = tmp_path / "hour.tar.gz"
    with tarfile.open(source, "w:gz") as bundle:
        for name, data in (("selected.log", b"selected-tail"), ("other.log", b"x" * 4096)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))

    def cross_device(*_):
        raise OSError("cross-device link")

    monkeypatch.setattr(hour_download.os, "link", cross_device)
    result = hour_download.pin_hour_sources([
        ({"id": "file", "bytes": 8}, {"id": "file", "archiveMember": "selected.log"}, source, False),
    ], tmp_path / "pinned")
    assert result[0][3] is True
    with tarfile.open(result[0][2]) as bundle:
        assert "other.log" not in bundle.getnames()
        assert bundle.extractfile("selected.log").read() == b"selected"


def test_rebuilt_hour_preserves_parts_when_session_sequences_restart(tmp_path):
    """目录丢失后恢复的分卷无会话时间，不能按重置的块序号打乱下载。"""
    sources = []
    for part, sequence in [(1, 1), (2, 50), (3, 1)]:
        path = tmp_path / f"{part}.tar.gz"
        with tarfile.open(path, "w:gz") as bundle:
            member = tarfile.TarInfo("source.log")
            member.size = 1
            bundle.addfile(member, io.BytesIO(str(part).encode()))
        file = {"id": str(part), "nodeId": "node", "taskId": "task", "hour": "hour",
                "segmentNumber": part, "firstSequence": sequence, "archiveMember": "source.log"}
        sources.append(({"id": str(part), "bytes": 1}, file, path, True))
    output = tmp_path / "output.tar.gz"
    hour_download.write_hour_archive(output, list(reversed(sources)))
    with tarfile.open(output) as bundle:
        assert b"".join(bundle.extractfile(m).read() for m in bundle.getmembers()) == b"123"


def test_search_caps_results_at_one_thousand(tmp_path):
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        source = tmp_path / "many.log"
        source.write_bytes(b"needle\n" * 1100)
        await repo.db.files.insert_one({"id": "many", "path": str(source), "status": "OPEN", "bytes": source.stat().st_size, "nodeId": "node"})
        async def audit(*_, **_kwargs): pass
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
        async def audit(*_, **_kwargs): pass
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


def test_writer_rechecks_execution_ownership_after_waiting_for_export_lock(tmp_path, monkeypatch):
    """旧 Worker 等待 flock 期间被取消后，取得锁也不能开始创建导出文件。"""
    async def scenario():
        settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
        repo = Repository(AsyncMongoMockClient().db, settings)
        async def audit(*_, **_kwargs): pass
        repo.audit = audit
        job = {"id": "late-writer", "kind": "DOWNLOAD", "status": "RUNNING", "files": []}
        await repo.db.jobs.insert_one(job)

        class LockAfterCancellation:
            def __init__(self, _repo, _identifier): pass
            async def acquire(self, *, blocking):
                assert blocking
                await repo.db.jobs.update_one({"id": "late-writer"}, {"$set": {"status": "CANCELLED"}})
                return True
            async def close(self): pass

        async def must_not_write(*_args):
            raise AssertionError("lost writer must not enter _download")

        monkeypatch.setattr(jobs, "ExportLock", LockAfterCancellation)
        monkeypatch.setattr(jobs, "_download", must_not_write)
        assert (await run_job(repo, job))["status"] == "CANCELLED"
        current = await repo.db.jobs.find_one({"id": "late-writer"})
        assert current.get("outputExecutionState") is None
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
