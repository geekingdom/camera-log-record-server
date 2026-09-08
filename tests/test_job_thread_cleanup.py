"""作业取消时必须等待快照和搜索工作线程退出的回归测试。"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs import jobs
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def repository(tmp_path):
    """创建本地节点仓库和一条可冻结的日志文件记录。"""
    settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
    repo = Repository(AsyncMongoMockClient().db, settings)

    async def audit(*_args):
        return None

    repo.audit = audit
    source = tmp_path / "source.log"
    source.write_bytes(b"needle\n")
    file = {
        "id": "file",
        "taskId": "task",
        "runId": "run",
        "sessionId": "session",
        "nodeId": "node",
        "status": "OPEN",
        "bytes": source.stat().st_size,
        "path": str(source),
    }
    await repo.db.files.insert_one(file)
    return repo, file


async def test_search_reserves_space_before_creating_snapshot(tmp_path, monkeypatch):
    """搜索与下载共用准入配额，空间不足时不能先写入快照。"""
    repo, file = await repository(tmp_path)
    monkeypatch.setattr(jobs, "TEMP_LIMIT", 10)
    monkeypatch.setattr(jobs, "SEARCH_SNAPSHOT_LIMIT", 8, raising=False)
    jobs._temp_reservations["other-download"] = 3
    job = {"id": "quota-search", "keyword": "needle", "files": [file],
           "start": "2026-09-08T00:00:00+00:00", "end": "2026-09-10T00:00:00+00:00"}
    try:
        with pytest.raises(ValueError, match="temporary export storage"):
            await jobs._search(repo, job)
        assert not (tmp_path / "exports" / ".tmp" / job["id"]).exists()
        assert job["id"] not in jobs._temp_reservations
    finally:
        jobs._temp_reservations.pop("other-download", None)


async def test_search_oversized_snapshot_cleans_files_and_reservation(tmp_path, monkeypatch):
    """压缩输出越界时必须失败并回收文件和搜索预留。"""
    repo, file = await repository(tmp_path)
    monkeypatch.setattr(jobs, "SEARCH_SNAPSHOT_LIMIT", 32, raising=False)
    job = {"id": "oversized-search", "keyword": "needle", "files": [file],
           "start": "2026-09-08T00:00:00+00:00", "end": "2026-09-10T00:00:00+00:00"}
    with pytest.raises(ValueError, match="snapshot storage limit"):
        await jobs._search(repo, job)
    assert not (tmp_path / "exports" / ".tmp" / job["id"]).exists()
    assert job["id"] not in jobs._temp_reservations


def test_archive_cancellation_waits_for_snapshot_thread_before_returning(tmp_path, monkeypatch):
    """取消快照时不得让后台线程继续向已经开始回收的临时目录写入。"""
    async def scenario():
        repo, file = await repository(tmp_path)
        entered, release = threading.Event(), threading.Event()

        def snapshot(_source, target, *_args, **_kwargs):
            entered.set()
            release.wait()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"snapshot")

        monkeypatch.setattr(jobs, "snapshot", snapshot)
        task = asyncio.create_task(jobs._archive(repo, file.copy(), tmp_path / "scratch", include_index=True))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_search_cancellation_signals_thread_and_waits_before_removing_scratch(tmp_path, monkeypatch):
    """搜索取消先通知工作线程，再等待线程退出后清理搜索快照目录。"""
    async def scenario():
        repo, file = await repository(tmp_path)
        entered, release, saw_cancelled, thread_exited = (threading.Event() for _ in range(4))
        cleanup_started = threading.Event()
        cleanup_after_thread = []
        job = {
            "id": "search",
            "kind": "SEARCH",
            "status": "RUNNING",
            "actor": "tester",
            "keyword": "needle",
            "start": datetime(2026, 9, 8, tzinfo=UTC).isoformat(),
            "end": datetime(2026, 9, 10, tzinfo=UTC).isoformat(),
            "files": [file.copy()],
        }
        await repo.db.jobs.insert_one(job)

        async def archive(*_args, **_kwargs):
            return tmp_path / "archive.tar.gz", True

        def search_limited(_path, _scanner, _file, _limit, cancelled, _archive_member=None):
            entered.set()
            release.wait()
            if cancelled():
                saw_cancelled.set()
            thread_exited.set()
            return []

        original_rmtree = jobs.shutil.rmtree

        def rmtree(path, *args, **kwargs):
            cleanup_started.set()
            cleanup_after_thread.append(thread_exited.is_set())
            return original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(jobs, "_archive", archive)
        monkeypatch.setattr(jobs, "_search_limited", search_limited)
        monkeypatch.setattr(jobs.shutil, "rmtree", rmtree)
        task = asyncio.create_task(jobs._search(repo, job))
        scratch = tmp_path / "exports" / ".tmp" / job["id"]
        cleanup_before_release = False
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            cleanup_before_release = await asyncio.to_thread(cleanup_started.wait, .05)
            if not cleanup_before_release:
                assert not task.done()
                assert scratch.is_dir()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not cleanup_before_release
        assert saw_cancelled.is_set()
        assert cleanup_after_thread == [True]
        assert not scratch.exists()
        assert job["id"] not in jobs._temp_reservations

    asyncio.run(scenario())


async def test_search_discards_each_temporary_snapshot_before_next_file(tmp_path, monkeypatch):
    """历史范围扩大时不能把全部搜索快照一直保留到整项作业结束。"""
    repo, file = await repository(tmp_path)
    previous = []

    async def archive(_repo, _file, scratch, **_kwargs):
        assert all(not path.exists() for path in previous)
        path = scratch / f"snapshot-{len(previous)}.tar.gz"
        path.write_bytes(b"temporary")
        previous.append(path)
        return path, True

    monkeypatch.setattr(jobs, "_archive", archive)
    monkeypatch.setattr(jobs, "_search_limited", lambda *_args: [])
    job = {"id": "sequential", "files": [file.copy(), file.copy()], "keyword": "needle",
        "start": "2026-09-08T00:00:00+00:00", "end": "2026-09-10T00:00:00+00:00"}
    assert (await jobs._search(repo, job))["results"] == []
    assert len(previous) == 2 and all(not path.exists() for path in previous)


async def test_download_cancel_keeps_reservation_until_snapshot_exits(tmp_path, monkeypatch):
    """下载取消后的快照线程不能绕过临时目录清理或提前释放并发配额。"""
    repo, file = await repository(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def snapshot(_source, target, *_args, **_kwargs):
        entered.set()
        release.wait()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"late snapshot")

    monkeypatch.setattr(jobs, "snapshot", snapshot)
    job = {"id": "download-cancel", "files": [file.copy()]}
    task = asyncio.create_task(jobs._download(repo, job))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and job["id"] in jobs._temp_reservations
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert job["id"] not in jobs._temp_reservations
    assert not (tmp_path / "exports" / ".tmp" / job["id"]).exists()
    assert not (tmp_path / "exports" / job["id"]).exists()


async def test_finished_cleanup_releases_reservation_without_waiting_for_admission_scan(monkeypatch):
    """释放已清理空间只需移除事件循环中的条目，不应被新作业的扫描锁阻塞。"""
    lock = asyncio.Lock()
    monkeypatch.setattr(jobs, "_temp_reservation_lock", lock)
    monkeypatch.setattr(jobs, "_temp_reservations", {"finished": 10})
    async with lock:
        await asyncio.wait_for(jobs._release_temp("finished"), .1)
    assert jobs._temp_reservations == {}
