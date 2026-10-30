"""索引补建取消必须等待独立进程 future 收尾，不能遗留 SQLite 临时写入。"""

import asyncio
from pathlib import Path

import pytest
from camera_logs.logs import compression


@pytest.mark.parametrize("failure", [False, True])
async def test_index_build_cancellation_drains_process_future_before_propagating(monkeypatch, failure):
    """重复取消也必须等 future 结算；后台失败不能掩盖原始取消。"""
    future = asyncio.get_running_loop().create_future()

    async def pending_submission(*_args):
        return await compression._wait_future(future)

    monkeypatch.setattr(compression, "_run_index_in_pool", pending_submission)
    task = asyncio.create_task(compression.build_search_index(Path("hour.tar.gz")))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    if failure:
        future.set_exception(OSError("synthetic index failure"))
    else:
        future.set_result(True)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert future.done()


async def test_timeout_shutdown_cancels_queued_index_without_restart_race(monkeypatch, tmp_path):
    """归档超时取消后不得由 finally 重启已排队索引，也不能遗留任务或关闭标记。"""
    archive_started = asyncio.Event()

    async def blocked_archive(*_arguments):
        archive_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(compression, "_run_in_pool", blocked_archive)
    monkeypatch.setattr(compression, "_run_index_in_pool",
                        lambda *_arguments: (_ for _ in ()).throw(AssertionError("关闭中不应启动索引")))
    archive = asyncio.create_task(compression.compress_hour([], tmp_path / "blocked.tar.gz"))
    await archive_started.wait()
    index = asyncio.create_task(compression.build_search_index(tmp_path / "queued.tar.gz"))
    await asyncio.sleep(0)

    await compression.shutdown_compression(timeout_seconds=.01)
    with pytest.raises(asyncio.CancelledError):
        await archive
    with pytest.raises(asyncio.CancelledError):
        await index
    assert not compression._archive_jobs and not compression._index_jobs
    assert compression._dispatcher is None and compression._index_dispatcher is None
    assert compression._pool is None and compression._index_pool is None
    assert not compression._scheduled_index_targets and not compression._scheduled_index_futures
    assert compression._closing is False
