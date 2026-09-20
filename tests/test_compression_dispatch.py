"""验证父进程压缩调度在单进程池前完成优先级、预算和取消收尾控制。"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from camera_logs.logs import compression
from camera_logs.node import performance_runtime


@pytest.fixture(autouse=True)
async def reset_dispatcher():
    """每项用独立调度状态，避免全局单进程池跨测试事件循环遗留引用。"""
    assert not compression._archive_jobs and not compression._index_jobs
    assert compression._dispatcher is None
    try:
        yield
    finally:
        await compression.shutdown_compression()
        compression._archive_jobs.clear()
        compression._index_jobs.clear()
        compression._dispatcher = None


async def test_waiting_archive_precedes_index_and_reads_budget_at_execution(monkeypatch, tmp_path):
    """已等待归档先于索引，且第二项获得槽位时才读取最新压力预算。"""
    first_started, release_first = asyncio.Event(), asyncio.Event()
    calls, pressured = [], False

    def settings():
        return (123, not pressured)

    async def run(function, *arguments):
        calls.append((function, arguments))
        if function is compression.publish_hour_archive and arguments[1].name == "first.tar.gz":
            first_started.set()
            await release_first.wait()
        return arguments[1] if function is compression.publish_hour_archive else True

    monkeypatch.setattr(performance_runtime, "background_settings", settings)
    monkeypatch.setattr(compression, "_run_in_pool", run)
    first = asyncio.create_task(compression.compress_hour([], tmp_path / "first.tar.gz"))
    await first_started.wait()
    index = asyncio.create_task(compression.build_search_index(tmp_path / "old.tar.gz"))
    second = asyncio.create_task(compression.compress_hour([], tmp_path / "second.tar.gz"))
    pressured = True
    release_first.set()
    await asyncio.gather(first, second, index)

    assert [function for function, _arguments in calls] == [
        compression.publish_hour_archive, compression.publish_hour_archive, compression.build_index,
    ]
    # 第二归档等待首项时策略切换为压力，实际执行才冻结读取限速和禁用内联索引。
    assert calls[1][1][-2:] == (123, False)


async def test_archive_with_another_waiting_skips_inline_index_before_low_priority_work(monkeypatch, tmp_path):
    """常态下有后续归档等待时，首项 READY 发布不等待自身 FTS 构建。"""
    calls = []

    async def run(function, *arguments):
        calls.append((function, arguments))
        return arguments[1] if function is compression.publish_hour_archive else True

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (999, True))
    monkeypatch.setattr(compression, "_run_in_pool", run)
    first = asyncio.create_task(compression.compress_hour([], tmp_path / "first.tar.gz"))
    second = asyncio.create_task(compression.compress_hour([], tmp_path / "second.tar.gz"))
    await asyncio.gather(first, second)

    assert calls[0][0] is compression.publish_hour_archive
    assert calls[0][1][-2:] == (None, False)
    assert calls[1][0] is compression.publish_hour_archive


async def test_cancelled_waiter_keeps_original_cancellation_after_job_failure():
    """已取消调用方等待后台失败收尾后仍抛取消，不能被工作异常覆盖。"""
    future = asyncio.get_running_loop().create_future()
    waiting = asyncio.create_task(compression._wait_for_job(future))
    await asyncio.sleep(0)
    waiting.cancel()
    await asyncio.sleep(0)
    assert not waiting.done()
    future.set_exception(OSError("synthetic background failure"))
    with pytest.raises(asyncio.CancelledError):
        await waiting


async def test_cancelled_process_wait_does_not_release_single_slot_early(monkeypatch):
    """取消调度器时仍等待线程中的等价底层工作结束，下一项不能提前使用槽位。"""
    started, release = threading.Event(), threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(compression, "_pool", pool)

    def blocking():
        started.set()
        release.wait(3)
        return "done"

    running = asyncio.create_task(compression._run_in_pool(blocking))
    assert await asyncio.to_thread(started.wait, 1)
    running.cancel()
    await asyncio.sleep(0)
    assert not running.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
