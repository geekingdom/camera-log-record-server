"""验证真实 spawn 压缩子进程在停止和崩溃后的可恢复边界。"""

import asyncio
import hashlib
import json
import multiprocessing
import os
import tarfile
import time
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest
from camera_logs.logs import archive_spool, compression


def _crash_compression_child() -> None:
    """在真正的 spawn 子进程中立即退出，模拟解释器或系统级异常退出。"""
    os._exit(23)


def _controlled_long_index(archive: Path, _rate: int | None) -> bool:
    """真实 spawn 索引子进程的受控长运行体，避免依赖机器速度制造竞态。"""
    started = Path(str(archive) + ".index-started")
    release = Path(str(archive) + ".index-release")
    started.touch()
    deadline = time.monotonic() + 10
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    return False


def _segment(log: Path, index: Path, body: bytes, index_body: bytes) -> dict:
    """构造与封存清单相同的成员摘要，供真实发布和恢复共同核验。"""
    return {
        "taskId": "task", "runId": "run", "sessionId": "session",
        "hourStart": "2026-10-12T00:00:00+00:00", "rawSize": len(body),
        "sha256": hashlib.sha256(body).hexdigest(), "firstSequence": 1, "lastSequence": 1,
        "logPath": str(log), "logName": log.name, "indexName": index.name,
        "indexSha256": hashlib.sha256(index_body).hexdigest(), "indexBytes": len(index_body),
    }


@pytest.fixture(autouse=True)
async def _clean_compression_scheduler():
    """每项隔离全局池和调度队列，防止真实子进程跨事件循环遗留。"""
    await compression.shutdown_compression(timeout_seconds=1)
    compression._archive_jobs.clear()
    compression._index_jobs.clear()
    compression._scheduled_targets.clear()
    compression._scheduled_futures.clear()
    compression._retry_not_before.clear()
    compression._retry_attempts.clear()
    yield
    await compression.shutdown_compression(timeout_seconds=1)
    compression._archive_jobs.clear()
    compression._index_jobs.clear()
    compression._scheduled_targets.clear()
    compression._scheduled_futures.clear()
    compression._retry_not_before.clear()
    compression._retry_attempts.clear()


async def _wait_for_child_process() -> object:
    """等待池实际派生子进程，避免以父进程提交成功误判压缩已经阻塞。"""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        pool = compression._pool
        processes = tuple(getattr(pool, "_processes", {}).values()) if pool is not None else ()
        if processes and any(process.is_alive() for process in processes):
            return next(process for process in processes if process.is_alive())
        await asyncio.sleep(0.02)
    raise AssertionError("spawn 压缩子进程未在限定时间内启动")


async def test_timeout_shutdown_terminates_real_child_and_recovers_spooled_archive(tmp_path):
    """五秒关闭会终止受阻子进程，保留原文和清单供新调度器完整恢复。"""
    body, index_body = b"camera body\n", b'{"sequence":1}\n'
    log, index = tmp_path / "part.log", tmp_path / "part.index.jsonl"
    target = tmp_path / "hour.tar.gz"
    log.write_bytes(body)
    os.mkfifo(index)
    spool = archive_spool.persist(target, [_segment(log, index, body, index_body)])

    publishing = asyncio.create_task(compression.compress_spool(spool))
    process = await _wait_for_child_process()
    started = time.monotonic()
    await compression.shutdown_compression(timeout_seconds=5)
    elapsed = time.monotonic() - started

    assert elapsed <= 8
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(publishing, 10)
    assert compression._pool is None
    await asyncio.to_thread(process.join, 1)
    assert not process.is_alive()
    assert spool.exists()
    assert Path(f"{target}.pending.json").exists()
    assert log.read_bytes() == body

    index.unlink()
    index.write_bytes(index_body)
    restored = await compression.compress_spool(spool)
    assert restored == target
    assert not spool.exists()
    assert not Path(f"{target}.pending.json").exists()
    assert not log.exists()
    with tarfile.open(target, "r:gz") as archive:
        member = archive.extractfile(log.name)
        assert member is not None
        assert member.read() == body
    metadata = json.loads(Path(f"{target}.metadata.json").read_text(encoding="utf-8"))
    assert metadata["members"] == [{key: value for key, value in _segment(log, index, body, index_body).items()
                                    if key != "logPath"}]


async def test_broken_process_pool_is_rebuilt_for_later_spooled_archive(tmp_path):
    """子进程异常退出仅结算当前作业，新池仍可发布后续已封存归档。"""
    with pytest.raises(BrokenProcessPool):
        await compression._run_in_pool(_crash_compression_child)
    assert compression._pool is None

    body, index_body = b"later camera body\n", b'{"sequence":2}\n'
    log, index = tmp_path / "later.log", tmp_path / "later.index.jsonl"
    target = tmp_path / "later.tar.gz"
    log.write_bytes(body)
    index.write_bytes(index_body)
    spool = archive_spool.persist(target, [_segment(log, index, body, index_body)])

    assert await compression.compress_spool(spool) == target
    assert compression._pool is not None
    with tarfile.open(target, "r:gz") as archive:
        member = archive.extractfile(log.name)
        assert member is not None
        assert hashlib.sha256(member.read()).hexdigest() == hashlib.sha256(body).hexdigest()


async def test_real_long_index_process_does_not_block_spooled_archive(tmp_path, monkeypatch):
    """索引池真实长运行时，归档使用另一进程池完成发布，关闭后两个池均可回收。"""
    index_target = tmp_path / "old-hour.tar.gz"
    monkeypatch.setattr(compression, "build_index", _controlled_long_index)
    indexing = asyncio.create_task(compression.build_search_index(index_target))
    started = Path(str(index_target) + ".index-started")
    deadline = time.monotonic() + 3
    while not started.exists() and time.monotonic() < deadline:
        await asyncio.sleep(.02)
    assert started.exists(), "索引子进程未启动"

    body, index_body = b"archive proceeds\n", b'{"sequence":5}\n'
    log, index = tmp_path / "parallel.log", tmp_path / "parallel.index.jsonl"
    target = tmp_path / "parallel.tar.gz"
    log.write_bytes(body)
    index.write_bytes(index_body)
    spool = archive_spool.persist(target, [_segment(log, index, body, index_body)])
    assert await asyncio.wait_for(compression.compress_spool(spool), 5) == target
    assert target.exists() and not spool.exists()
    assert compression._pool is not None and compression._index_pool is not None

    Path(str(index_target) + ".index-release").touch()
    assert await indexing is False
    await compression.shutdown_compression(timeout_seconds=2)
    assert compression._pool is None and compression._index_pool is None


async def test_stalled_child_is_terminated_and_later_spool_uses_new_pool(tmp_path, monkeypatch):
    """运行期无进展 watchdog 终止真实卡住发布，spool 与原始物料可由新池恢复。"""
    monkeypatch.setattr(compression, "PROCESS_PROGRESS_TIMEOUT_SECONDS", 2)
    body, index_body = b"recovered after stall\n", b'{"sequence":3}\n'
    log, index = tmp_path / "stalled.log", tmp_path / "stalled.index.jsonl"
    target = tmp_path / "stalled.tar.gz"
    log.write_bytes(body)
    os.mkfifo(index)
    spool = archive_spool.persist(target, [_segment(log, index, body, index_body)])

    with pytest.raises(TimeoutError, match="持续无进展"):
        await compression.compress_spool(spool)
    assert compression._pool is None
    assert spool.exists()
    assert Path(f"{target}.pending.json").exists()
    assert log.read_bytes() == body

    index.unlink()
    index.write_bytes(index_body)
    assert await compression.compress_spool(spool) == target
    assert not spool.exists()
    assert not log.exists()


async def test_stalled_first_spool_does_not_block_later_spools_or_its_retry(tmp_path, monkeypatch):
    """队首 FIFO 卡死后同一调度器继续发布后项，修复首项后按退避重试。"""
    monkeypatch.setattr(compression, "PROCESS_PROGRESS_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(compression, "ARCHIVE_RETRY_BASE_SECONDS", 0.2)
    bad_log, bad_index = tmp_path / "bad.log", tmp_path / "bad.index.jsonl"
    bad_target = tmp_path / "bad.tar.gz"
    body, index_body = b"body\n", b'{"sequence":4}\n'
    bad_log.write_bytes(body)
    os.mkfifo(bad_index)
    bad_spool = archive_spool.persist(bad_target, [_segment(bad_log, bad_index, body, index_body)])
    normal = []
    for number in range(2):
        log, index = tmp_path / f"normal-{number}.log", tmp_path / f"normal-{number}.index.jsonl"
        target = tmp_path / f"normal-{number}.tar.gz"
        log.write_bytes(body)
        index.write_bytes(index_body)
        normal.append((target, archive_spool.persist(target, [_segment(log, index, body, index_body)])))

    failed = compression.schedule_pending_archive(bad_spool)
    later = [compression.schedule_pending_archive(spool) for _target, spool in normal]
    assert failed is not None and all(future is not None for future in later)
    with pytest.raises(TimeoutError, match="持续无进展"):
        await failed
    await asyncio.gather(*later)
    assert all(target.exists() and not spool.exists() for target, spool in normal)

    bad_index.unlink()
    bad_index.write_bytes(index_body)
    await asyncio.sleep(0.22)
    retried = compression.schedule_pending_archive(bad_spool)
    assert retried is not None
    assert await retried == bad_target


async def test_watchdog_uses_submission_time_when_pool_progress_is_old(monkeypatch):
    """空闲池的旧心跳不能让新提交作业在第一次一秒轮询时被误杀。"""
    progress = compression.create_progress(multiprocessing.get_context("spawn"))
    assert not hasattr(progress[0], "get_lock")
    progress[0].value = time.monotonic() - 600
    future = asyncio.get_running_loop().create_future()
    waiter = asyncio.create_task(compression._wait_future(
        future, progress=progress, pool=None, submitted_at=time.monotonic(),
    ))
    await asyncio.sleep(1.05)
    assert not waiter.done()
    future.set_result("completed")
    assert await waiter == "completed"


def test_terminate_pool_refuses_new_writer_when_killed_process_stays_alive(monkeypatch):
    """不可中断子进程在 kill 后仍存活时保留故障池，不能启动并发写者。"""
    monkeypatch.setattr(compression, "PROCESS_TERMINATE_GRACE_SECONDS", 0)

    class StuckProcess:
        def __init__(self):
            self.calls = []

        def is_alive(self):
            return True

        def terminate(self):
            self.calls.append("terminate")

        def kill(self):
            self.calls.append("kill")

        def join(self, timeout=None):
            self.calls.append(("join", timeout))

    class Pool:
        def __init__(self, process):
            self._processes = {1: process}
            self.shutdown_called = False

        def shutdown(self, **_kwargs):
            self.shutdown_called = True

    process = StuckProcess()
    pool = Pool(process)
    with pytest.raises(RuntimeError, match="拒绝启动新的写入进程"):
        compression._terminate_pool(pool)
    assert process.calls[0] == "terminate"
    assert "kill" in process.calls
    assert not pool.shutdown_called
