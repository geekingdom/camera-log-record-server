"""验证真实 spawn 压缩子进程在停止和崩溃后的可恢复边界。"""

import asyncio
import hashlib
import json
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
