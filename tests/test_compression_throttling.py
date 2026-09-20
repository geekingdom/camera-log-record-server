"""压力模式压缩必须对所有正文读取阶段累计限速。"""

import asyncio
import hashlib
import threading

import pytest
from camera_logs.logs import archive_access, compression
from camera_logs.node import performance_runtime


def test_pressure_archive_limits_hash_copy_and_final_verification(tmp_path, monkeypatch):
    """索引摘要、原文摘要、tar 复制和发布复核都必须经过同一个后台限速器。"""
    consumed = []

    class RecordingLimiter:
        def __init__(self, rate):
            assert rate == 123

        def consume(self, size):
            consumed.append(size)

    log, index = tmp_path / "part.log", tmp_path / "part.index.jsonl"
    log_data, index_data = b"device-log\n", b'{"sequence":1}\n'
    log.write_bytes(log_data)
    index.write_bytes(index_data)
    segment = {
        "taskId": "task", "runId": "run", "sessionId": "session", "hourStart": "2026-09-20T00:00:00+00:00",
        "rawSize": len(log_data), "sha256": hashlib.sha256(log_data).hexdigest(), "firstSequence": 1,
        "lastSequence": 1, "logPath": str(log), "logName": log.name, "indexName": index.name,
        "indexSha256": hashlib.sha256(index_data).hexdigest(), "indexBytes": len(index_data),
    }
    monkeypatch.setattr(archive_access, "ReadLimiter", RecordingLimiter)

    compression.publish_hour_archive([segment], tmp_path / "hour.tar.gz", background_rate=123, create_index=False)

    # 索引摘要一次，正文在摘要、tar 复制、归档复核三个阶段各一次。
    assert sum(consumed) == len(index_data) + len(log_data) * 3


async def test_cancelled_compression_shutdown_keeps_pool_until_thread_exit(monkeypatch):
    """关闭协程被取消后仍等待进程池关闭，不能先清引用而留下后台进程。"""
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    class BlockingPool:
        def shutdown(self, *, wait):
            assert wait is True
            loop.call_soon_threadsafe(entered.set)
            release.wait(3)

    pool = BlockingPool()
    monkeypatch.setattr(compression, "_pool", pool)
    closing = asyncio.create_task(compression.shutdown_compression())
    await asyncio.wait_for(entered.wait(), 1)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    assert compression._pool is pool
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert compression._pool is None


async def test_failed_process_pool_submission_finishes_archive_lifecycle(monkeypatch, tmp_path):
    """进程池已损坏时提交同步失败，也必须移除已登记的归档等待计时。"""
    started, finished = [], []

    async def broken_submission(*_args):
        raise RuntimeError("synthetic broken process pool")

    monkeypatch.setattr(compression, "_run_in_pool", broken_submission)
    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (50 * 1024 * 1024, True))
    monkeypatch.setattr(performance_runtime, "archive_started", started.append)
    monkeypatch.setattr(performance_runtime, "archive_finished", finished.append)

    with pytest.raises(RuntimeError, match="broken process pool"):
        await compression.compress_hour([], tmp_path / "hour.tar.gz")
    assert len(started) == len(finished) == 1
    assert started[0] is finished[0]
