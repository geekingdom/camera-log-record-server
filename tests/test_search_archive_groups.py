"""共享小时归档的分组检索与运行中结果发布回归。"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs import jobs
from camera_logs.logs.search_archives import ReadyArchiveGroup
from camera_logs.logs.search_index import build_index
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def _repository(tmp_path):
    """建立一个含两个连续成员及其独立旁路索引的已发布小时包。"""
    settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
    repo = Repository(AsyncMongoMockClient().db, settings)

    async def audit(*_args, **_kwargs):
        return None

    repo.audit = audit
    archive = tmp_path / "shared.tar.gz"
    chunks = (("first.log", b"needle first\n", 1), ("second.log", b"needle second\n", 2))
    with tarfile.open(archive, "w:gz") as bundle:
        for name, payload, _sequence in chunks:
            entry = tarfile.TarInfo(name)
            entry.size = len(payload)
            bundle.addfile(entry, io.BytesIO(payload))
    files = []
    stamp = datetime(2026, 9, 18, tzinfo=UTC)
    for position, (name, payload, sequence) in enumerate(chunks):
        index = tmp_path / f"{name}.index.jsonl"
        index.write_text(json.dumps({
            "offset": 0, "length": len(payload), "receivedAt": stamp.isoformat(),
            "sequence": sequence, "taskId": "task", "runId": "run", "sessionId": "session", "nodeId": "node",
        }) + "\n", encoding="utf-8")
        file = {
            "id": f"file-{position}", "taskId": "task", "runId": "run", "sessionId": "session", "nodeId": "node",
            "status": "READY", "bytes": len(payload), "path": str(archive), "archiveMember": name,
            "archiveGroupId": "shared", "indexPath": str(index),
        }
        files.append(file)
        await repo.db.files.insert_one(file)
    return repo, files


async def test_search_scans_shared_archive_once_and_publishes_first_member_results(tmp_path, monkeypatch):
    """同包成员复用一次 tar 解压，首成员命中无需等待整项作业才可查询。"""
    repo, files = await _repository(tmp_path)
    job = {
        "id": "grouped-search", "kind": "SEARCH", "status": "RUNNING", "actor": "tester",
        "keyword": "needle", "start": (datetime(2026, 9, 18, tzinfo=UTC) - timedelta(minutes=1)).isoformat(),
        "end": (datetime(2026, 9, 18, tzinfo=UTC) + timedelta(minutes=1)).isoformat(), "files": files,
    }
    await repo.db.jobs.insert_one(job)
    opened = 0
    original_open = tarfile.open

    def count_open(*args, **kwargs):
        nonlocal opened
        opened += 1
        return original_open(*args, **kwargs)

    first_published, release_publish = asyncio.Event(), asyncio.Event()
    original_publish = jobs.publish_search_results

    async def publish_after_first(*args, **kwargs):
        published = await original_publish(*args, **kwargs)
        first_published.set()
        await release_publish.wait()
        return published

    monkeypatch.setattr(jobs.tarfile, "open", count_open)
    monkeypatch.setattr(jobs, "publish_search_results", publish_after_first)
    running = asyncio.create_task(jobs.run_job(repo, job))
    await asyncio.wait_for(first_published.wait(), 1)
    interim = await repo.db.jobs.find_one({"id": job["id"]})
    assert interim["status"] == "RUNNING"
    assert "executionToken" not in interim
    assert interim["resultCount"] == 1
    assert interim["results"][0]["fileId"] == "file-0"
    release_publish.set()
    result = await running
    assert result["status"] == "SUCCEEDED"
    assert [(item["fileId"], item["offset"]) for item in result["results"]] == [
        ("file-0", 0), ("file-1", 0),
    ]
    assert opened == 1


async def test_search_checks_cancellation_between_shared_archive_members(tmp_path, monkeypatch):
    """同包复用句柄也必须在每个成员前停止，已发布前缀不能被取消收尾清空。"""
    repo, files = await _repository(tmp_path)
    job = {
        "id": "grouped-cancel", "kind": "SEARCH", "status": "RUNNING", "actor": "tester",
        "keyword": "needle", "start": (datetime(2026, 9, 18, tzinfo=UTC) - timedelta(minutes=1)).isoformat(),
        "end": (datetime(2026, 9, 18, tzinfo=UTC) + timedelta(minutes=1)).isoformat(), "files": files,
    }
    await repo.db.jobs.insert_one(job)
    scans = []
    original_scan = ReadyArchiveGroup.scan_member

    def count_scan(self, file, *args, **kwargs):
        scans.append(file["id"])
        return original_scan(self, file, *args, **kwargs)

    async def cancel_before_second(*_args):
        return bool(scans)

    monkeypatch.setattr(ReadyArchiveGroup, "scan_member", count_scan)
    monkeypatch.setattr(jobs, "_cancelled", cancel_before_second)
    result = await jobs.run_job(repo, job)
    stored = await repo.db.jobs.find_one({"id": job["id"]})
    assert result["status"] == stored["status"] == "CANCELLED"
    assert scans == ["file-0"]
    assert stored["resultCount"] == 1 and stored["results"][0]["fileId"] == "file-0"


async def test_search_keeps_frozen_order_when_archive_members_are_physically_reversed(tmp_path):
    """旧包物理成员逆序时仅为未命中成员重开定位，结果不丢失也不按物理顺序改写。"""
    repo, files = await _repository(tmp_path)
    archive = Path(files[0]["path"])
    payloads = []
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            payloads.append((member.name, source.extractfile(member).read()))
    with tarfile.open(archive, "w:gz") as target:
        for name, payload in reversed(payloads):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            target.addfile(member, io.BytesIO(payload))
    job = {"id": "reversed-search", "kind": "SEARCH", "status": "RUNNING", "actor": "tester",
           "keyword": "needle", "start": (datetime(2026, 9, 18, tzinfo=UTC) - timedelta(minutes=1)).isoformat(),
           "end": (datetime(2026, 9, 18, tzinfo=UTC) + timedelta(minutes=1)).isoformat(), "files": files}
    await repo.db.jobs.insert_one(job)
    result = await jobs.run_job(repo, job)
    assert result["status"] == "SUCCEEDED"
    assert [item["fileId"] for item in result["results"]] == ["file-0", "file-1"]


async def test_single_ready_member_does_not_rebuild_a_snapshot(tmp_path, monkeypatch):
    """单分卷同样直接读取已发布归档，不退回重新压缩路径。"""
    repo, files = await _repository(tmp_path)

    async def unexpected_snapshot(*_args, **_kwargs):
        raise AssertionError("READY本地成员不应创建快照")

    monkeypatch.setattr(jobs, "_archive", unexpected_snapshot)
    job = {"id": "single-search", "kind": "SEARCH", "status": "RUNNING", "actor": "tester",
           "keyword": "needle", "start": "2026-09-17T23:59:00+00:00", "end": "2026-09-18T00:01:00+00:00",
           "files": files[:1]}
    await repo.db.jobs.insert_one(job)
    result = await jobs.run_job(repo, job)
    assert result["status"] == "SUCCEEDED" and result["resultCount"] == 1


async def test_persisted_index_skips_all_raw_reads_for_absent_keyword(tmp_path, monkeypatch):
    """完整作业只用可信索引排除无匹配归档，仍正确提交零结果与100%进度。"""
    repo, files = await _repository(tmp_path)
    assert build_index(Path(files[0]["path"]))

    def forbidden_raw_read(*_args, **_kwargs):
        raise AssertionError("已确认阴性的归档不得再读取正文")

    monkeypatch.setattr(ReadyArchiveGroup, "open", forbidden_raw_read)
    job = {"id": "indexed-empty", "kind": "SEARCH", "status": "RUNNING", "actor": "tester",
           "keyword": "missing-keyword", "start": "2026-09-17T23:59:00+00:00", "end": "2026-09-18T00:01:00+00:00",
           "files": files}
    await repo.db.jobs.insert_one(job)
    result = await jobs.run_job(repo, job)
    assert result["status"] == "SUCCEEDED" and result["resultCount"] == 0
    assert result["results"] == [] and result["progress"] == 100 and not result["truncated"]
