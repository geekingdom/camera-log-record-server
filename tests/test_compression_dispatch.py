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


async def test_running_index_does_not_delay_new_archive_and_splits_budget(monkeypatch, tmp_path):
    """索引已运行时，新 spool 立即使用独立归档槽，预算固定拆成八二比例。"""
    index_started, release_index, archive_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def run_archive(function, *arguments):
        calls.append(("archive", function, arguments))
        archive_started.set()
        return arguments[1]

    async def run_index(function, *arguments):
        calls.append(("index", function, arguments))
        index_started.set()
        await release_index.wait()
        return True

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (100, True))
    monkeypatch.setattr(compression, "_run_in_pool", run_archive)
    monkeypatch.setattr(compression, "_run_index_in_pool", run_index)
    index = asyncio.create_task(compression.build_search_index(tmp_path / "old.tar.gz"))
    await index_started.wait()
    archive = asyncio.create_task(compression.compress_hour([], tmp_path / "new.tar.gz"))
    await asyncio.wait_for(archive_started.wait(), .2)
    assert await archive == tmp_path / "new.tar.gz"
    assert calls[0] == ("index", compression.build_index, (tmp_path / "old.tar.gz", 20))
    assert calls[1][0:2] == ("archive", compression.publish_hour_archive)
    assert calls[1][2][-2:] == (80, False)
    release_index.set()
    assert await index is True


async def test_archive_with_another_waiting_skips_inline_index_before_low_priority_work(monkeypatch, tmp_path):
    """常态下有后续归档等待时，首项 READY 发布不等待自身 FTS 构建。"""
    calls = []

    async def run(function, *arguments):
        calls.append((function, arguments))
        return arguments[1] if function is compression.publish_hour_archive else True

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (999, False))
    monkeypatch.setattr(compression, "_run_in_pool", run)
    first = asyncio.create_task(compression.compress_hour([], tmp_path / "first.tar.gz"))
    second = asyncio.create_task(compression.compress_hour([], tmp_path / "second.tar.gz"))
    await asyncio.gather(first, second)

    assert calls[0][0] is compression.publish_hour_archive
    # 生效预算固定八二拆分：归档只用八成且永远禁止内联索引，剩余两成
    # 预留给独立低优先级索引池，不能超卖磁盘读取。
    assert calls[0][1][-2:] == (799, False)
    assert calls[1][0] is compression.publish_hour_archive


async def test_published_archives_enqueue_at_most_eight_deduplicated_indexes_and_idle_pool_catches_up(monkeypatch, tmp_path):
    """百个连续发布不会无限积压索引；空闲后有限队列可排空，同目标只登记一次。"""
    started, release = asyncio.Event(), asyncio.Event()
    indexed = []

    async def run_index(_function, target, _rate):
        indexed.append(target)
        if len(indexed) == 1:
            started.set()
            await release.wait()
        return True

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (100, True))
    monkeypatch.setattr(compression, "_run_index_in_pool", run_index)
    targets = [tmp_path / f"{number:03d}.tar.gz" for number in range(100)]
    admitted = [compression.schedule_published_index(target) for target in targets]
    duplicate = compression.schedule_published_index(targets[0])
    await started.wait()

    assert sum(future is not None for future in admitted) == compression.MAX_SCHEDULED_INDEXES
    assert duplicate is admitted[0]
    assert len(compression._scheduled_index_targets) == compression.MAX_SCHEDULED_INDEXES
    assert len(compression._index_jobs) == compression.MAX_SCHEDULED_INDEXES - 1

    release.set()
    await asyncio.gather(*(future for future in admitted if future is not None))
    assert len(indexed) == compression.MAX_SCHEDULED_INDEXES
    assert not compression._scheduled_index_targets
    assert not compression._scheduled_index_futures

    for target in targets[compression.MAX_SCHEDULED_INDEXES:]:
        future = compression.schedule_published_index(target)
        assert future is not None
        assert await future is True
    assert len(indexed) == len(targets)


async def test_published_archive_leaves_index_to_maintenance_when_pressure_disables_it(monkeypatch, tmp_path):
    """压力策略关闭索引时，归档仍 READY，且不把低优先级索引排入独立池。"""
    target = tmp_path / "published.tar.gz"

    async def run_archive(_function, *_arguments):
        return target

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (100, False))
    monkeypatch.setattr(compression, "_run_in_pool", run_archive)
    assert await compression.compress_hour([], target) == target
    assert not compression._scheduled_index_targets and not compression._index_jobs


async def test_successful_archive_detaches_bounded_index_submission(monkeypatch, tmp_path):
    """归档成功只登记索引而不等待，调用方先拿到 READY 结果，索引在后续独立执行。"""
    target = tmp_path / "published.tar.gz"
    index_started, release_index = asyncio.Event(), asyncio.Event()

    async def run_archive(_function, *_arguments):
        return target

    async def run_index(_function, *_arguments):
        index_started.set()
        await release_index.wait()
        return True

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (100, True))
    monkeypatch.setattr(compression, "_run_in_pool", run_archive)
    monkeypatch.setattr(compression, "_run_index_in_pool", run_index)
    assert await asyncio.wait_for(compression.compress_hour([], target), .2) == target
    await index_started.wait()
    assert target in compression._scheduled_index_targets
    release_index.set()
    while compression._scheduled_index_targets:
        await asyncio.sleep(0)


async def test_failed_or_cancelled_index_releases_target_identity(monkeypatch, tmp_path):
    """失败或取消不保留旧 target，后续维护可重新登记而不会复活旧 Future。"""
    target = tmp_path / "retryable.tar.gz"

    async def fail(_function, *_arguments):
        raise OSError("synthetic index error")

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (100, True))
    monkeypatch.setattr(compression, "_run_index_in_pool", fail)
    first = compression.schedule_published_index(target)
    assert first is not None
    with pytest.raises(OSError, match="synthetic index error"):
        await first
    assert target not in compression._scheduled_index_targets
    assert target not in compression._scheduled_index_futures

    async def succeed(_function, *_arguments):
        return True

    monkeypatch.setattr(compression, "_run_index_in_pool", succeed)
    second = compression.schedule_published_index(target)
    assert second is not None and second is not first
    assert await second is True


async def test_queued_indexes_defer_when_pressure_changes_after_first_starts(monkeypatch, tmp_path):
    """已运行索引可完成，但后续候选在压力禁用后不得再提交独立索引进程。"""
    started, release = asyncio.Event(), asyncio.Event()
    enabled, calls = [True], []

    async def run_index(_function, target, _rate):
        calls.append(target)
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (100, enabled[0]))
    monkeypatch.setattr(compression, "_run_index_in_pool", run_index)
    first = compression.schedule_published_index(tmp_path / "first.tar.gz")
    assert first is not None
    await started.wait()
    second = compression.schedule_published_index(tmp_path / "second.tar.gz")
    assert second is not None
    enabled[0] = False
    release.set()

    assert await first is True
    assert await second is False
    assert calls == [tmp_path / "first.tar.gz"]
    assert not compression._scheduled_index_targets


async def test_queued_index_defers_to_new_archive_without_stopping_running_index(monkeypatch, tmp_path):
    """新归档进入队列后，索引仅让位未启动候选，已运行索引与归档各自完成。"""
    index_started, release_index, archive_started, release_archive = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event())
    enabled, indexed = [True], []

    async def run_index(_function, target, _rate):
        indexed.append(target)
        index_started.set()
        await release_index.wait()
        return True

    async def run_archive(_function, *_arguments):
        archive_started.set()
        await release_archive.wait()
        return tmp_path / "published.tar.gz"

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (100, enabled[0]))
    monkeypatch.setattr(compression, "_run_index_in_pool", run_index)
    monkeypatch.setattr(compression, "_run_in_pool", run_archive)
    first = compression.schedule_published_index(tmp_path / "first.tar.gz")
    assert first is not None
    await index_started.wait()
    second = compression.schedule_published_index(tmp_path / "second.tar.gz")
    assert second is not None
    archive = asyncio.create_task(compression.compress_hour([], tmp_path / "published.tar.gz"))
    await archive_started.wait()
    release_index.set()

    assert await first is True
    assert await second is False
    assert indexed == [tmp_path / "first.tar.gz"]
    enabled[0] = False
    release_archive.set()
    assert await archive == tmp_path / "published.tar.gz"


async def test_inflight_target_replacement_can_be_requeued_after_incomplete_index(monkeypatch, tmp_path):
    """同 target 归档更新时在途旧索引可失败，完成后身份释放让维护补建新版本。"""
    started, release = asyncio.Event(), asyncio.Event()
    target = tmp_path / "replaced.tar.gz"

    async def stale(_function, *_arguments):
        started.set()
        await release.wait()
        return False

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (100, True))
    monkeypatch.setattr(compression, "_run_index_in_pool", stale)
    old = compression.schedule_published_index(target)
    assert old is not None
    await started.wait()
    assert compression.schedule_published_index(target) is old
    release.set()
    assert await old is False
    assert target not in compression._scheduled_index_targets

    async def current(_function, *_arguments):
        return True

    monkeypatch.setattr(compression, "_run_index_in_pool", current)
    fresh = compression.schedule_published_index(target)
    assert fresh is not None and fresh is not old
    assert await fresh is True


async def test_archive_lifecycle_logs_safe_owner_and_durations(monkeypatch, tmp_path, caplog):
    """归档父进程记录固定生命周期字段，不把路径或正文带入运行日志。"""
    target = tmp_path / "sensitive-target.tar.gz"
    segments = [{"taskId": "task-1", "runId": "run-1", "sessionId": "session-1",
                 "logPath": "/private/device-output.log", "password": "must-not-log"}]

    async def run(_function, *_arguments):
        return target

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (999, False))
    monkeypatch.setattr(compression, "_run_in_pool", run)
    with caplog.at_level("INFO", logger="camera_logs.logs.compression"):
        assert await compression.compress_hour(segments, target) == target

    records = [record for record in caplog.records if record.getMessage() == "归档作业状态"
               and record.context["kind"] == "archive"]
    assert [record.context["phase"] for record in records] == ["QUEUED", "RUNNING", "SUCCEEDED"]
    for record in records:
        assert set(record.context) == {
            "kind", "phase", "archiveKey", "taskId", "runId", "sessionId", "queueMs", "runMs", "errorType", "retryAfterSeconds",
        }
        assert len(record.context["archiveKey"]) == 16
        assert record.context["taskId"] == "task-1"
        assert record.context["runId"] == "run-1"
        assert record.context["sessionId"] == "session-1"
        assert record.context["queueMs"] >= 0 and record.context["runMs"] >= 0
        assert str(target) not in str(record.context)
        assert "must-not-log" not in str(record.context)


async def test_index_lifecycle_logs_safe_queue_and_execution_diagnostics(monkeypatch, tmp_path, caplog):
    """独立索引池也记录有界状态，不把归档路径或异常正文暴露到运行日志。"""
    async def run(_function, *_arguments):
        return True

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (100, True))
    monkeypatch.setattr(compression, "_run_index_in_pool", run)
    target = tmp_path / "private-hour.tar.gz"
    with caplog.at_level("INFO", logger="camera_logs.logs.compression"):
        assert await compression.build_search_index(target) is True

    records = [record.context for record in caplog.records if record.getMessage() == "归档作业状态"
               and record.context["kind"] == "index"]
    assert [record["phase"] for record in records] == ["QUEUED", "RUNNING", "SUCCEEDED"]
    assert all(record["kind"] == "index" and len(record["archiveKey"]) == 16 for record in records)
    assert all(str(target) not in str(record) for record in records)


async def test_distinct_index_targets_have_distinct_safe_archive_keys(monkeypatch, tmp_path, caplog):
    """索引任务也必须按目标区分实例键，不能把全部小时聚合成同一作业。"""
    async def run(_function, *_arguments):
        return True

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (100, True))
    monkeypatch.setattr(compression, "_run_index_in_pool", run)
    with caplog.at_level("INFO", logger="camera_logs.logs.compression"):
        await compression.build_search_index(tmp_path / "first.tar.gz")
        await compression.build_search_index(tmp_path / "second.tar.gz")

    queued = [record.context["archiveKey"] for record in caplog.records
              if record.getMessage() == "归档作业状态" and record.context["phase"] == "QUEUED"]
    assert len(queued) == 2 and queued[0] != queued[1]


async def test_index_false_result_logs_incomplete_not_succeeded(monkeypatch, tmp_path, caplog):
    """索引构建未发布时保留原文回退，并记录固定原因而不能伪报成功。"""
    async def run(_function, *_arguments):
        return False

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (100, True))
    monkeypatch.setattr(compression, "_run_index_in_pool", run)
    with caplog.at_level("INFO", logger="camera_logs.logs.compression"):
        assert await compression.build_search_index(tmp_path / "unsupported.tar.gz") is False

    records = [record.context for record in caplog.records if record.getMessage() == "归档作业状态"
               and record.context["kind"] == "index"]
    assert [record["phase"] for record in records] == ["QUEUED", "RUNNING", "INCOMPLETE"]
    assert records[-1]["errorType"] == "INDEX_NOT_PUBLISHED"


async def test_spooled_archive_lifecycle_never_reads_manifest_only_for_logging(monkeypatch, tmp_path, caplog):
    """spool 调度不能为日志额外读取磁盘，未知归属用安全占位符表示。"""
    from camera_logs.logs.archive_spool import persist

    target = tmp_path / "target.tar.gz"
    spool = persist(target, [{"taskId": "task-2", "runId": "run-2", "sessionId": "session-2",
                              "logPath": "/private/device-output.log", "logName": "part.log"}])

    async def run(_function, *_arguments):
        return target

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (999, False))
    monkeypatch.setattr(compression, "_run_in_pool", run)
    monkeypatch.setattr(compression, "load_archive_spool",
                        lambda _spool: (_ for _ in ()).throw(AssertionError("日志不应读取 spool")))
    with caplog.at_level("INFO", logger="camera_logs.logs.compression"):
        future = compression.schedule_pending_archive(spool)
        assert future is not None
        assert await future == target

    records = [record.context for record in caplog.records if record.getMessage() == "归档作业状态"
               and record.context["kind"] == "spool"]
    assert [record["phase"] for record in records] == ["QUEUED", "RUNNING", "SUCCEEDED"]
    assert all((record["taskId"], record["runId"], record["sessionId"]) == ("-", "-", "-")
               for record in records)
    assert len({record["archiveKey"] for record in records}) == 1
    assert all("/private/device-output.log" not in str(record) for record in records)


async def test_dispatcher_cancellation_logs_archive_once_without_retry_warning(monkeypatch, tmp_path, caplog):
    """正常关闭的已运行归档只结算一次取消状态，不被记录成后台失败。"""
    started, release = asyncio.Event(), asyncio.Event()
    spool = tmp_path / "queued.pending.json"
    spool.write_text("spool")

    async def run(_function, *_arguments):
        started.set()
        await release.wait()

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (999, False))
    monkeypatch.setattr(compression, "_run_in_pool", run)
    with caplog.at_level("INFO", logger="camera_logs.logs.compression"):
        future = compression.schedule_pending_archive(spool)
        assert future is not None
        await started.wait()
        dispatcher = compression._dispatcher
        assert dispatcher is not None
        dispatcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatcher
        assert future.cancelled()

    records = [record for record in caplog.records if record.getMessage() == "归档作业状态"]
    assert [record.context["phase"] for record in records] == ["QUEUED", "RUNNING", "CANCELLED"]
    assert not [record for record in caplog.records if "后台归档失败" in record.getMessage()]


async def test_one_spool_failure_does_not_block_later_archive(monkeypatch, tmp_path):
    """单个不可变 spool 发布失败只结算自身，后续归档仍必须获得唯一执行槽位。"""
    calls = []

    async def run(function, *arguments):
        calls.append(arguments[0])
        if len(calls) == 1:
            raise OSError("synthetic spool failure")
        return tmp_path / "published.tar.gz"

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (999, False))
    monkeypatch.setattr(compression, "_run_in_pool", run)
    first = compression._enqueue("spool", tmp_path / "first.pending.json")
    second = compression._enqueue("spool", tmp_path / "second.pending.json")
    with pytest.raises(OSError, match="synthetic spool failure"):
        await first
    assert await second == tmp_path / "published.tar.gz"
    assert calls == [tmp_path / "first.pending.json", tmp_path / "second.pending.json"]


async def test_dispatcher_cancellation_releases_queued_spools_for_a_restart(monkeypatch, tmp_path):
    """停止中的调度器也结算排队 spool，下一启动轮可继续发布全部持久任务。"""
    first_started, release_first = asyncio.Event(), asyncio.Event()
    published = []
    clock = [0.0]
    spools = [tmp_path / f"queued-{number}.pending.json" for number in range(8)]
    for spool in spools:
        spool.write_text("spool")

    async def run(_function, spool, *_arguments):
        if spool == spools[0]:
            first_started.set()
            await release_first.wait()
        published.append(spool)
        return tmp_path / f"{spool.stem}.tar.gz"

    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (999, False))
    monkeypatch.setattr(compression, "_run_in_pool", run)
    monkeypatch.setattr(compression.time, "monotonic", lambda: clock[0])
    scheduled = [compression.schedule_pending_archive(spool) for spool in spools]
    assert all(future is not None for future in scheduled)
    await first_started.wait()

    dispatcher = compression._dispatcher
    assert dispatcher is not None
    dispatcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatcher
    results = await asyncio.gather(*scheduled, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert not compression._scheduled_targets
    assert not compression._scheduled_futures
    assert performance_runtime.archive_snapshot()["runningJobs"] == 0

    # 取消仍沿用既有退避，物理 spool 保留，下一轮维护可在退避后重新登记。
    clock[0] = compression.ARCHIVE_RETRY_BASE_SECONDS
    restarted = [compression.schedule_pending_archive(spool) for spool in spools]
    assert all(future is not None for future in restarted)
    release_first.set()
    await asyncio.gather(*restarted)
    assert published == spools


async def test_failed_spools_back_off_so_later_spool_gets_a_fair_slot(monkeypatch, tmp_path):
    """前八个失败 spool 退避后，下一维护轮必须能发布排在其后的正常 spool。"""
    tick = [0.0]
    monkeypatch.setattr(compression.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(compression, "ARCHIVE_RETRY_BASE_SECONDS", 10)
    monkeypatch.setattr(compression, "ARCHIVE_RETRY_MAX_SECONDS", 40)
    monkeypatch.setattr(performance_runtime, "background_settings", lambda: (999, False))

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


async def test_background_archive_failure_logs_one_safe_lifecycle_retry_warning(monkeypatch, tmp_path, caplog):
    """后台失败须留下安全退避字段，不能把路径或异常正文写入日志。"""
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

    records = [record for record in caplog.records if record.getMessage() == "归档作业状态"]
    failed = [record.context for record in records if record.context["phase"] == "FAILED"]
    assert failed == [{"kind": "spool", "phase": "FAILED", "archiveKey": failed[0]["archiveKey"], "taskId": "-", "runId": "-", "sessionId": "-",
                       "queueMs": failed[0]["queueMs"], "runMs": failed[0]["runMs"],
                       "errorType": "OSError", "retryAfterSeconds": 30}]
    record = next(record for record in records if record.context["phase"] == "FAILED")
    assert record.levelname == "WARNING"
    assert record.errorFrames and all(set(frame) == {"file", "line", "function"} for frame in record.errorFrames)
    assert "password=secret" not in str(failed[0])
    assert str(spool) not in str(failed[0])


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


async def test_live_sealing_cannot_consume_recovery_reserved_slots(tmp_path, monkeypatch):
    """持续实时封存不能抢走历史恢复名额；历史项进入同一队列后正常完成。"""
    release = asyncio.Event()
    completed = []

    async def run(_function, spool, *_arguments):
        await release.wait()
        completed.append(spool)
        return spool

    monkeypatch.setattr(compression, "_run_in_pool", run)
    live = []
    for number in range(6):
        spool = tmp_path / f"live-{number}"
        spool.touch()
        live.append(compression.schedule_pending_archive(spool, recovery=False))
    old = tmp_path / "old"
    old.touch()
    try:
        for number in range(20):
            fresh = tmp_path / f"fresh-{number}"
            fresh.touch()
            assert compression.schedule_pending_archive(fresh, recovery=False) is None
        recovering = compression.schedule_pending_archive(old, recovery=True)
        assert recovering is not None
        assert len(compression._scheduled_targets) == 7
    finally:
        release.set()
    await asyncio.gather(*live, recovering)
    assert old in completed
