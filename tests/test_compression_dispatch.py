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
    compression._retry_not_before.clear()
    compression._retry_attempts.clear()
    assert not compression._archive_jobs and not compression._index_jobs
    assert compression._dispatcher is None
    try:
        yield
    finally:
        await compression.shutdown_compression()
        compression._archive_jobs.clear()
        compression._index_jobs.clear()
        compression._retry_not_before.clear()
        compression._retry_attempts.clear()
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


async def test_one_spool_failure_does_not_block_later_archive(monkeypatch, tmp_path):
    """单个不可变 spool 发布失败只结算自身，后续归档仍必须获得唯一执行槽位。"""
    calls = []

    async def run(function, *arguments):
        calls.append(arguments[0])
        if len(calls) == 1:
            raise OSError("synthetic spool failure")
        return tmp_path / "published.tar.gz"

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (999, True))
    monkeypatch.setattr(compression, "_run_in_pool", run)
    first = compression._enqueue("spool", tmp_path / "first.pending.json")
    second = compression._enqueue("spool", tmp_path / "second.pending.json")
    with pytest.raises(OSError, match="synthetic spool failure"):
        await first
    assert await second == tmp_path / "published.tar.gz"
    assert calls == [tmp_path / "first.pending.json", tmp_path / "second.pending.json"]


async def test_failed_spools_back_off_so_later_spool_gets_a_fair_slot(monkeypatch, tmp_path):
    """前八个失败 spool 退避后，下一维护轮必须能发布排在其后的正常 spool。"""
    tick = [0.0]
    monkeypatch.setattr(compression.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(compression, "ARCHIVE_RETRY_BASE_SECONDS", 10)
    monkeypatch.setattr(compression, "ARCHIVE_RETRY_MAX_SECONDS", 40)
    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (999, True))

    async def run(_function, spool, *_arguments):
        if spool.name.startswith("bad"):
            raise OSError("corrupt immutable spool")
        return tmp_path / "published.tar.gz"

    monkeypatch.setattr(compression, "_run_in_pool", run)
    failed_paths = [tmp_path / f"bad-{number}.pending.json" for number in range(8)]
    normal_path = tmp_path / "normal.pending.json"
    for spool in [*failed_paths, normal_path]:
        spool.write_text("spool")
    failed = [compression.schedule_pending_archive(path) for path in failed_paths]
    assert all(future is not None for future in failed)
    assert compression.schedule_pending_archive(normal_path) is None
    results = await asyncio.gather(*failed, return_exceptions=True)
    assert all(isinstance(result, OSError) for result in results)
    assert all(compression._retry_not_before[path] == 10 for path in compression._retry_not_before)

    normal = compression.schedule_pending_archive(normal_path)
    assert normal is not None
    assert await normal == tmp_path / "published.tar.gz"
    assert all(compression.schedule_pending_archive(path) is None for path in failed_paths)

    tick[0] = 10
    retry = compression.schedule_pending_archive(failed_paths[0])
    assert retry is not None
    with pytest.raises(OSError, match="corrupt immutable spool"):
        await retry
    assert compression._retry_not_before[failed_paths[0]] == 30


async def test_background_archive_failure_logs_one_sanitized_retry_record(monkeypatch, tmp_path, caplog):
    """后台失败须留下单条可定位退避证据，不能把异常正文或归档内容写入日志。"""
    spool = tmp_path / "failed.pending.json"
    spool.write_text("spool")

    async def fail(*_arguments):
        raise OSError("device password=secret raw body")

    monkeypatch.setattr(compression, "_run_in_pool", fail)
    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (999, True))
    with caplog.at_level("WARNING", logger="camera_logs.logs.compression"):
        future = compression.schedule_pending_archive(spool)
        assert future is not None
        with pytest.raises(OSError, match="password=secret"):
            await future

    entries = [record.message for record in caplog.records if "后台归档失败" in record.message]
    assert entries == [f"后台归档失败 target={spool} errorType=OSError retryAfterSeconds=30"]
    assert "password=secret" not in entries[0]


async def test_retry_state_is_bounded_and_removed_when_spool_disappears(monkeypatch, tmp_path):
    """损坏 spool 的退避状态既限制指数也限制路径数量，删除后不长期占用内存。"""
    tick = [0.0]
    monkeypatch.setattr(compression.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(compression, "MAX_ARCHIVE_RETRY_ENTRIES", 2)
    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (999, True))

    async def fail(_function, _spool, *_arguments):
        raise OSError("corrupt immutable spool")

    monkeypatch.setattr(compression, "_run_in_pool", fail)
    paths = [tmp_path / f"bad-{number}.pending.json" for number in range(3)]
    for path in paths:
        path.write_text("spool")
        retry = compression.schedule_pending_archive(path)
        assert retry is not None
        with pytest.raises(OSError, match="corrupt immutable spool"):
            await retry
    assert len(compression._retry_not_before) == len(compression._retry_attempts) == 2
    assert paths[0] not in compression._retry_not_before

    compression._retry_attempts[paths[1]] = compression.ARCHIVE_RETRY_MAX_EXPONENT + 1
    compression._retry_not_before[paths[1]] = 0
    retry = compression.schedule_pending_archive(paths[1])
    assert retry is not None
    with pytest.raises(OSError, match="corrupt immutable spool"):
        await retry
    assert compression._retry_attempts[paths[1]] == compression.ARCHIVE_RETRY_MAX_EXPONENT + 1

    paths[1].unlink()
    assert compression.schedule_pending_archive(paths[1]) is None
    assert paths[1] not in compression._retry_not_before
    assert paths[1] not in compression._retry_attempts


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
