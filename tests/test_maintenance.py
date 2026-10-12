"""维护回归：默认保留期、下载保护及归档发布后目录登记中断的恢复。"""

import asyncio
import hashlib
import io
import json
import os
import tarfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.logs import maintenance
from camera_logs.logs.maintenance import (
    _archive_id,
    apply_retention,
    cleanup_exports,
    recover_orphan_archives,
)
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


def repository(root):
    """隔离文件系统和数据库，避免维护测试访问真实采集目录。"""
    return Repository(AsyncMongoMockClient().db, Settings(
        log_root=root, node_id="node", retention_days=7,
        encryption_key=Fernet.generate_key().decode()))


def archive_at(root, *, wrong_digest=False):
    """构造共享小时归档、外部索引和成员清单，模拟目录回写前退出。"""
    path = root / "task" / "run" / "session" / "part-001.tar.gz"
    path.parent.mkdir(parents=True)
    raw = b"ordered device output\n"
    index = b'{"offset":0}\n'
    manifest = {"taskId": "task", "runId": "run", "sessionId": "session",
        "hourStart": now().replace(minute=0, second=0, microsecond=0).isoformat(),
        "rawSize": len(raw), "sha256": "invalid" if wrong_digest else hashlib.sha256(raw).hexdigest(),
        "firstSequence": 1, "lastSequence": 1, "logName": "part-001.log", "indexName": "part-001.index.jsonl",
        "indexSha256": hashlib.sha256(index).hexdigest(), "indexBytes": len(index)}
    with tarfile.open(path, "w:gz") as bundle:
        info = tarfile.TarInfo("part-001.log")
        info.size = len(raw)
        bundle.addfile(info, io.BytesIO(raw))
    (path.parent / manifest["indexName"]).write_bytes(index)
    Path(str(path) + ".metadata.json").write_text(json.dumps({"formatVersion": 2, "members": [manifest]}))
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


async def test_retention_keeps_shared_archive_while_another_member_is_protected(tmp_path):
    """共享小时包必须等待全部成员均可删除，不能由一个过期记录提前 unlink。"""
    repo = repository(tmp_path)
    path = tmp_path / "shared.tar.gz"
    path.write_bytes(b"published")
    metadata = Path(str(path) + ".metadata.json")
    first_index, second_index = tmp_path / "first.index.jsonl", tmp_path / "second.index.jsonl"
    metadata.write_text("{}")
    first_index.write_text("first")
    second_index.write_text("second")
    old_hour = (now() - timedelta(days=8)).isoformat()
    await repo.db.files.insert_many([
        {"id": "expired-member", "nodeId": "node", "status": "READY", "hour": old_hour, "path": str(path),
         "indexPath": str(first_index)},
        {"id": "protected-member", "nodeId": "node", "status": "READY", "hour": old_hour, "path": str(path),
         "retainUntil": now() + timedelta(hours=1), "indexPath": str(second_index)},
    ])
    result = await apply_retention(repo)
    assert result["removed"] == 0
    assert path.exists()
    assert await repo.db.files.count_documents({"path": str(path)}) == 2
    await repo.db.files.update_one({"id": "protected-member"}, {"$set": {"retainUntil": now() - timedelta(hours=1)}})
    assert (await apply_retention(repo))["removed"] == 2
    assert not path.exists()
    assert not metadata.exists()
    assert not first_index.exists()
    assert not second_index.exists()
    assert await repo.db.files.count_documents({"path": str(path)}) == 0


async def test_retention_preserves_archive_while_compression_pending_journal_exists(tmp_path):
    """事务清单代表未完成发布，保留任务不得删除其 tar、索引或目录记录。"""
    repo = repository(tmp_path)
    path = tmp_path / "pending.tar.gz"
    path.write_bytes(b"published")
    Path(str(path) + ".pending.json").write_text('{"formatVersion":1,"segments":[]}')
    await repo.db.files.insert_one({"id": "pending", "nodeId": "node", "status": "READY",
                                    "hour": (now() - timedelta(days=8)).isoformat(), "path": str(path)})
    assert (await apply_retention(repo))["removed"] == 0
    assert path.exists()
    assert await repo.db.files.find_one({"id": "pending"})


async def test_recovery_completes_inactive_pending_archive_before_catalog_registration(tmp_path, monkeypatch):
    """节点启动只登记旧 pending 事务，不能等待积压归档后才继续。"""
    repo = repository(tmp_path)
    path, member = archive_at(tmp_path)
    raw = path.parent / member["logName"]
    raw.write_bytes(b"ordered device output\n")
    pending_member = member | {"logPath": str(raw)}
    Path(str(path) + ".pending.json").write_text(json.dumps({"formatVersion": 1, "segments": [pending_member]}))
    calls = []

    monkeypatch.setattr("camera_logs.logs.maintenance.schedule_recovery_archive", lambda target: calls.append(target))
    assert await recover_orphan_archives(repo) == 0
    assert calls == [path]
    assert Path(str(path) + ".pending.json").exists()
    assert raw.exists()


async def test_recovery_discovers_pending_journal_before_tar_publication(tmp_path, monkeypatch):
    """tar 尚未替换的旧 pending 也只能有界登记，不能阻塞节点启动。"""
    repo = repository(tmp_path)
    path, member = archive_at(tmp_path)
    raw = path.parent / member["logName"]
    raw.write_bytes(b"ordered device output\n")
    path.unlink()
    Path(str(path) + ".metadata.json").unlink()
    pending_member = member | {"logPath": str(raw)}
    Path(str(path) + ".pending.json").write_text(json.dumps({"formatVersion": 1, "segments": [pending_member]}))
    calls = []

    monkeypatch.setattr("camera_logs.logs.maintenance.schedule_recovery_archive", lambda target: calls.append(target))
    assert await recover_orphan_archives(repo) == 0
    assert calls == [path]
    assert not path.exists() and Path(str(path) + ".pending.json").exists()
    assert raw.exists()


async def test_recovery_queues_immutable_spool_without_waiting_for_publication(tmp_path, monkeypatch):
    """重启只把未发布 spool 交给有界调度器，不能在启动时同步压缩积压小时包。"""
    from camera_logs.logs.archive_spool import persist

    repo = repository(tmp_path)
    target = tmp_path / "device" / "hour.tar.gz"
    raw = target.parent / "part.log"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"queued\n")
    spool = persist(target, [{"taskId": "task", "runId": "run", "sessionId": "session",
                              "hourStart": now().isoformat(), "rawSize": 7,
                              "sha256": hashlib.sha256(b"queued\n").hexdigest(), "firstSequence": 1,
                              "lastSequence": 1, "logPath": str(raw), "logName": raw.name,
                              "indexName": "part.index.jsonl", "indexSha256": "x", "indexBytes": 0}])
    queued = []
    monkeypatch.setattr("camera_logs.logs.maintenance.schedule_pending_archive", lambda path: queued.append(path))

    assert await recover_orphan_archives(repo) == 0
    assert queued == [spool]
    assert spool.exists()


async def test_recovery_scan_reports_unique_disk_pending_archive_targets(tmp_path, monkeypatch):
    """维护以归档目标计数，同目标 spool 与旧 pending 不在看板重复成两个作业。"""
    from camera_logs.logs.archive_spool import persist

    repo = repository(tmp_path)
    target = tmp_path / "device" / "hour.tar.gz"
    target.parent.mkdir(parents=True)
    segment = {"taskId": "task", "runId": "run", "sessionId": "session"}
    persist(target, [segment])
    persist(target.parent / "later.tar.gz", [segment])
    Path(f"{target}.pending.json").write_text(json.dumps({"formatVersion": 1, "segments": [segment]}))
    reported = []
    monkeypatch.setattr("camera_logs.node.performance_runtime.archive_spool_snapshot",
                        lambda pending, oldest: reported.append((pending, oldest)))
    monkeypatch.setattr("camera_logs.logs.maintenance.schedule_pending_archive", lambda _spool: None)
    monkeypatch.setattr("camera_logs.logs.maintenance.schedule_recovery_archive", lambda _target: None)

    assert await recover_orphan_archives(repo) == 0
    assert reported and reported[-1][0] == 2
    assert reported[-1][1] >= 0


async def test_recovery_rebuilds_only_proven_raw_index_spool(tmp_path, monkeypatch):
    """旧内存 pending 丢失时，完整停机证明和连续索引才能重建可恢复 spool。"""
    repo = repository(tmp_path)
    raw = tmp_path / "device" / "camera-task" / "2026-10-12" / "08" / "camera-192.0.2.1-20261012080000-20261012090000-part-000001.log"
    raw.parent.mkdir(parents=True)
    body = b"first\nsecond\n"
    raw.write_bytes(body)
    raw.with_suffix(".index.jsonl").write_text(
        '{"sequence":31,"offset":0,"length":6,"receivedAt":"2026-10-12T00:00:00+00:00"}\n'
        '{"sequence":32,"offset":6,"length":7,"receivedAt":"2026-10-12T00:00:01+00:00"}\n')
    proof = {"token": "pre-stop-token", "nodeId": "node", "taskId": "task", "runId": "run",
             "generation": 4, "sessionId": "session", "confirmedAt": now().isoformat()}
    await repo.db.runs.insert_one({"id": "run", "taskId": "task", "nodeId": "node", "generation": 4,
                                   "sessionId": "session", "deploymentClosedProof": proof})
    await repo.db.files.insert_one({"id": "raw", "path": str(raw), "taskId": "task", "runId": "run",
                                    "sessionId": "session", "nodeId": "node", "status": "OPEN",
                                    "deploymentClosedProof": proof | {"fileId": "raw", "path": str(raw),
                                                                      "watermark": len(body)}})
    queued = []
    monkeypatch.setattr("camera_logs.logs.maintenance.schedule_pending_archive", lambda spool: queued.append(spool))

    assert await recover_orphan_archives(repo) == 0
    assert len(queued) == 1
    spool = queued[0]
    from camera_logs.logs.archive_spool import load
    target, segments = load(spool)
    assert target.name.endswith(".tar.gz")
    assert segments[0]["sha256"] == hashlib.sha256(body).hexdigest()
    assert segments[0]["firstSequence"] == 31 and segments[0]["lastSequence"] == 32
    assert raw.exists() and raw.with_suffix(".index.jsonl").exists()
    recovered_file = await repo.db.files.find_one({"id": "raw"})
    assert recovered_file["deploymentClosedProof"]["archiveRecoveredAt"]


async def test_recovery_preserves_raw_when_proof_or_index_is_incomplete(tmp_path, monkeypatch):
    """缺少精确证明或索引不连续时保留原始文件，绝不按目录猜测归档。"""
    repo = repository(tmp_path)
    raw = tmp_path / "device" / "camera-task" / "2026-10-12" / "08" / "camera-part-000001.log"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"recoverable\n")
    raw.with_suffix(".index.jsonl").write_text(
        '{"sequence":1,"offset":1,"length":12,"receivedAt":"2026-10-12T00:00:00+00:00"}\n')
    proof = {"token": "pre-stop-token", "nodeId": "node", "taskId": "task", "runId": "run",
             "generation": 4, "sessionId": "session", "confirmedAt": now().isoformat()}
    await repo.db.runs.insert_one({"id": "run", "taskId": "task", "nodeId": "node", "generation": 4,
                                   "sessionId": "session", "deploymentClosedProof": proof})
    await repo.db.files.insert_one({"id": "raw", "path": str(raw), "taskId": "task", "runId": "run",
                                    "sessionId": "session", "nodeId": "node", "status": "OPEN"})
    queued = []
    monkeypatch.setattr("camera_logs.logs.maintenance.schedule_pending_archive", lambda spool: queued.append(spool))

    assert await recover_orphan_archives(repo) == 0
    assert queued == []
    assert raw.exists() and raw.with_suffix(".index.jsonl").exists()
    assert not list(tmp_path.rglob("*.pending.*.json"))
    incomplete_run = await repo.db.runs.find_one({"id": "run"})
    assert "archiveRecoveredAt" not in incomplete_run["deploymentClosedProof"]


async def test_recovery_repairs_open_record_after_archive_publication(tmp_path):
    repo = repository(tmp_path)
    path, manifest = archive_at(tmp_path)
    identifier = _archive_id(tmp_path, path.parent / manifest["logName"])
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


async def test_recovery_scans_archive_tree_in_job_thread(tmp_path, monkeypatch):
    """大日志目录的枚举必须离开 Worker 事件循环，恢复后的目录登记保持可用。"""
    repo = repository(tmp_path)
    path, manifest = archive_at(tmp_path)
    main_thread = threading.get_ident()
    scan_threads = []
    original_scan = maintenance._scanned_orphan_archives

    def scanned(root):
        scan_threads.append(threading.get_ident())
        return original_scan(root)

    async def threaded(function, *args, **kwargs):
        return await asyncio.to_thread(function, *args, **kwargs)

    monkeypatch.setattr(maintenance, "_scanned_orphan_archives", scanned)
    monkeypatch.setattr(maintenance, "job_thread", threaded)

    assert await recover_orphan_archives(repo) == 1
    assert scan_threads and all(identifier != main_thread for identifier in scan_threads)
    record = await repo.db.files.find_one({"id": _archive_id(tmp_path, path.parent / manifest["logName"])})
    assert record is not None and record["status"] == "READY"


async def test_recovery_registers_each_shared_archive_member_with_stable_log_identity(tmp_path):
    """恢复共享小时包时，每个日志成员保留其自身任务、运行和会话身份。"""
    repo = repository(tmp_path)
    path = tmp_path / "shared" / "2026" / "09" / "08" / "12" / "hour.tar.gz"
    path.parent.mkdir(parents=True)
    hour = now().replace(minute=0, second=0, microsecond=0).isoformat()
    members = []
    with tarfile.open(path, "w:gz") as bundle:
        for number, task in enumerate(("task-a", "task-b"), start=1):
            log_name, index_name = f"task-part-{number:06d}.log", f"task-part-{number:06d}.index.jsonl"
            raw = f"{task}\n".encode()
            index = (json.dumps({"offset": 0, "receivedAt": hour}) + "\n").encode()
            info = tarfile.TarInfo(log_name); info.size = len(raw); bundle.addfile(info, io.BytesIO(raw))
            (path.parent / index_name).write_bytes(index)
            members.append({"taskId": task, "runId": f"run-{number}", "sessionId": f"session-{number}",
                            "hourStart": hour, "rawSize": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                            "firstSequence": number, "lastSequence": number, "logName": log_name, "indexName": index_name,
                            "indexSha256": hashlib.sha256(index).hexdigest(), "indexBytes": len(index)})
    Path(str(path) + ".metadata.json").write_text(json.dumps({"formatVersion": 2, "members": members}))

    assert await recover_orphan_archives(repo) == 2
    records = [item async for item in repo.db.files.find({"path": str(path)})]
    assert {record["segmentNumber"] for record in records} == {1, 2}
    assert all(record["firstReceivedAt"] == hour for record in records)
    assert {record["id"] for record in records} == {
        _archive_id(tmp_path, path.parent / member["logName"]) for member in members
    }
    assert {(record["taskId"], record["runId"], record["sessionId"], record["archiveMember"]) for record in records} == {
        (member["taskId"], member["runId"], member["sessionId"], member["logName"]) for member in members
    }


async def test_recovery_rejects_checksum_mismatch_and_preserves_archive(tmp_path):
    repo = repository(tmp_path)
    path, _ = archive_at(tmp_path, wrong_digest=True)
    assert await recover_orphan_archives(repo) == 0
    assert await repo.db.files.count_documents({}) == 0
    assert path.exists()


async def test_recovery_supports_legacy_manifest_archive_without_sidecar(tmp_path):
    """已有单分卷归档缺少 v2 sidecar 时仍可恢复，避免维护循环持续报错。"""
    repo = repository(tmp_path)
    path = tmp_path / "legacy.tar.gz"
    stamp = "2026-09-08T04:12:34+00:00"
    raw, index = b"legacy\n", (json.dumps({"offset": 0, "receivedAt": stamp}) + "\n").encode()
    manifest = {"taskId": "task", "runId": "run", "sessionId": "session", "hourStart": now().isoformat(),
                "rawSize": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "firstSequence": 1, "lastSequence": 1,
                "indexBytes": len(index), "indexSha256": hashlib.sha256(index).hexdigest()}
    with tarfile.open(path, "w:gz") as bundle:
        for name, content in (("legacy.log", raw), ("legacy.index.jsonl", index), ("manifest.json", json.dumps(manifest).encode())):
            info = tarfile.TarInfo(name); info.size = len(content); bundle.addfile(info, io.BytesIO(content))
    assert await recover_orphan_archives(repo) == 1
    record = await repo.db.files.find_one({})
    assert record["status"] == "READY"
    assert record["archiveMember"] == "legacy.log"
    assert record["firstReceivedAt"] == stamp
    assert record["segmentNumber"] == 0
    assert "indexPath" not in record


async def test_recovery_does_not_resurrect_retention_claim(tmp_path):
    repo = repository(tmp_path)
    path, _ = archive_at(tmp_path)
    await repo.db.files.insert_one({"id": _archive_id(tmp_path, path.parent / "part-001.log"), "status": "DELETING"})
    assert await recover_orphan_archives(repo) == 0
    assert (await repo.db.files.find_one({}))["status"] == "DELETING"


async def test_recovery_preserves_mismatched_session_identity(tmp_path):
    repo = repository(tmp_path)
    path, _ = archive_at(tmp_path)
    await repo.db.files.insert_one({"id": _archive_id(tmp_path, path.parent / "part-001.log"), "status": "OPEN",
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


async def test_export_cleanup_does_not_delete_a_running_download_only_because_directory_mtime_is_old(tmp_path):
    """复现旧实现按目录 mtime 删除仍在写入下载产物的错误，运行态必须保留。"""
    repo = repository(tmp_path)
    output = tmp_path / "exports" / "running-job"
    output.mkdir(parents=True)
    (output / "partial.tar.gz").write_bytes(b"partial")
    old = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(output, (old, old))
    await repo.db.jobs.insert_one({"id": "running-job", "nodeId": "node", "kind": "DOWNLOAD",
                                   "status": "RUNNING", "expiresAt": now() - timedelta(days=1),
                                   "completedAt": now() - timedelta(days=1)})

    assert await cleanup_exports(repo) == 0
    assert output.exists()


async def test_export_cleanup_removes_only_expired_completed_terminal_local_downloads(tmp_path):
    """过期的本节点终态下载产物按作业记录清理，不依赖目录修改时间。"""
    repo = repository(tmp_path)
    for status in ("SUCCEEDED", "FAILED", "EXPIRED"):
        output = tmp_path / "exports" / status.lower()
        output.mkdir(parents=True)
        await repo.db.jobs.insert_one({"id": output.name, "nodeId": "node", "kind": "DOWNLOAD",
                                       "status": status, "expiresAt": now() - timedelta(seconds=1),
                                       "completedAt": now() - timedelta(seconds=2),
                                       "outputExecutionState": "CLOSED", "outputWriterNodeId": "node",
                                       "outputWriterClosedAt": now() - timedelta(seconds=2), "outputLockProtocol": 1})

    assert await cleanup_exports(repo) == 3
    assert not any((tmp_path / "exports" / status.lower()).exists()
                   for status in ("SUCCEEDED", "FAILED", "EXPIRED"))


async def test_export_cleanup_preserves_jobs_without_all_terminal_expired_local_requirements(tmp_path):
    """缺记录、非下载、其它节点、取消、未到期或缺完成时间的目录都不可删除。"""
    repo = repository(tmp_path)
    protected = {
        "missing": None,
        "search": {"nodeId": "node", "kind": "SEARCH", "status": "SUCCEEDED"},
        "remote": {"nodeId": "other", "kind": "DOWNLOAD", "status": "SUCCEEDED"},
        "cancelled": {"nodeId": "node", "kind": "DOWNLOAD", "status": "CANCELLED"},
        "queued": {"nodeId": "node", "kind": "DOWNLOAD", "status": "QUEUED"},
        "running": {"nodeId": "node", "kind": "DOWNLOAD", "status": "RUNNING"},
        "not-expired": {"nodeId": "node", "kind": "DOWNLOAD", "status": "SUCCEEDED",
                        "expiresAt": now() + timedelta(hours=1)},
        "no-completion": {"nodeId": "node", "kind": "DOWNLOAD", "status": "SUCCEEDED", "completedAt": None},
        "damaged-time": {"nodeId": "node", "kind": "DOWNLOAD", "status": "SUCCEEDED",
                         "expiresAt": "not-a-datetime"},
    }
    for identifier, fields in protected.items():
        output = tmp_path / "exports" / identifier
        output.mkdir(parents=True)
        if fields is not None:
            await repo.db.jobs.insert_one({"id": identifier, "expiresAt": now() - timedelta(seconds=1),
                                           "completedAt": now() - timedelta(seconds=2), **fields})

    assert await cleanup_exports(repo) == 0
    assert all((tmp_path / "exports" / identifier).exists() for identifier in protected)


async def test_export_cleanup_rejects_symlinked_directory_even_when_its_job_is_eligible(tmp_path):
    """清理只能作用于 exports 下同名直系真实目录，软链接不能指向根外目录。"""
    repo = repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "exports" / "eligible"
    link.parent.mkdir()
    link.symlink_to(outside, target_is_directory=True)
    await repo.db.jobs.insert_one({"id": "eligible", "nodeId": "node", "kind": "DOWNLOAD",
                                   "status": "SUCCEEDED", "expiresAt": now() - timedelta(seconds=1),
                                   "completedAt": now() - timedelta(seconds=2),
                                   "outputExecutionState": "CLOSED", "outputWriterNodeId": "node",
                                   "outputWriterClosedAt": now() - timedelta(seconds=2), "outputLockProtocol": 1})

    assert await cleanup_exports(repo) == 0
    assert link.is_symlink() and outside.exists()


async def test_export_cleanup_rejects_a_symlinked_log_root(tmp_path):
    """即使作业已到期，配置日志根为软链接时也不能沿链接删除真实产物。"""
    actual_root = tmp_path / "actual-root"
    output = actual_root / "exports" / "eligible"
    output.mkdir(parents=True)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(actual_root, target_is_directory=True)
    repo = repository(linked_root)
    await repo.db.jobs.insert_one({"id": "eligible", "nodeId": "node", "kind": "DOWNLOAD",
                                   "status": "SUCCEEDED", "expiresAt": now() - timedelta(seconds=1),
                                       "completedAt": now() - timedelta(seconds=2),
                                       "outputExecutionState": "CLOSED", "outputWriterNodeId": "node",
                                       "outputWriterClosedAt": now() - timedelta(seconds=2), "outputLockProtocol": 1})

    assert await cleanup_exports(repo) == 0
    assert output.exists()


async def test_export_cleanup_keeps_failed_lookup_and_continues_with_next_eligible_directory(tmp_path, monkeypatch):
    """单个数据库读取失败必须保留原目录，但不能阻断后续已到期产物的清理。"""
    repo = repository(tmp_path)
    failed, eligible = tmp_path / "exports" / "failed", tmp_path / "exports" / "eligible"
    failed.mkdir(parents=True)
    eligible.mkdir()
    await repo.db.jobs.insert_one({"id": "eligible", "nodeId": "node", "kind": "DOWNLOAD",
                                   "status": "SUCCEEDED", "expiresAt": now() - timedelta(seconds=1),
                                   "completedAt": now() - timedelta(seconds=2),
                                   "outputExecutionState": "CLOSED", "outputWriterNodeId": "node",
                                   "outputWriterClosedAt": now() - timedelta(seconds=2), "outputLockProtocol": 1})
    original_find_one = repo.db.jobs.find_one

    async def fail_first(query, *args, **kwargs):
        if query == {"id": "failed"}:
            raise RuntimeError("synthetic database failure")
        return await original_find_one(query, *args, **kwargs)

    monkeypatch.setattr(repo.db.jobs, "find_one", fail_first)

    assert await cleanup_exports(repo) == 1
    assert failed.exists() and not eligible.exists()
