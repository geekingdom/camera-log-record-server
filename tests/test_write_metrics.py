"""验证单调时钟批次测量、有界窗口以及正在等待写入的可观测性。"""

from datetime import UTC, datetime

import pytest
from camera_logs.collection.collector import Collector
from camera_logs.common.write_metrics import WriteLatency


def test_latency_includes_batch_wait_and_expires_without_wall_clock():
    clock = [10.0]
    metrics = WriteLatency(clock=lambda: clock[0])
    metrics.begin()
    clock[0] += .1
    metrics.begin()
    clock[0] += .15
    assert metrics.snapshot()["pendingMs"] == pytest.approx(250)
    metrics.finish()
    assert metrics.snapshot()["p99Ms"] == pytest.approx(250)
    assert metrics.snapshot()["samples"] == 1
    assert metrics.snapshot()["pendingMs"] == 0
    clock[0] += 61
    assert metrics.snapshot()["samples"] == 0
    assert metrics.snapshot()["p99Ms"] == 0


def test_histogram_keeps_slow_samples_above_one_percent_beyond_old_sample_cap():
    clock = [0.0]
    metrics = WriteLatency(clock=lambda: clock[0])
    for _ in range(100):
        metrics.begin()
        clock[0] += .5
        metrics.finish()
    for _ in range(6900):
        metrics.begin()
        clock[0] += .001
        metrics.finish()
    snapshot = metrics.snapshot()
    assert snapshot["samples"] == 7000
    assert snapshot["p99Ms"] == 500
    assert snapshot["windowSeconds"] == 60


def test_histogram_expires_only_after_bucket_end_passes_window_boundary():
    clock = [0.0]
    metrics = WriteLatency(clock=lambda: clock[0])
    metrics.begin()
    clock[0] = .25
    metrics.finish()

    clock[0] = 60.999
    assert metrics.snapshot()["samples"] == 1
    clock[0] = 61.001
    assert metrics.snapshot()["samples"] == 0


def test_histogram_bucket_count_stays_bounded_over_long_runtime():
    clock = [0.0]
    metrics = WriteLatency(clock=lambda: clock[0])
    for second in range(62):
        metrics.begin()
        clock[0] = float(second)
        metrics.finish()

    assert len(metrics._buckets) == 61
    assert metrics.snapshot()["samples"] == 61


def test_pending_write_does_not_become_healthy_when_completed_samples_expire():
    clock = [0.0]
    metrics = WriteLatency(clock=lambda: clock[0])
    metrics.begin()
    clock[0] = 70
    assert metrics.snapshot()["pendingMs"] == 70000
    assert metrics.snapshot()["samples"] == 0


async def test_collector_measures_batch_wait_and_write_but_not_websocket_callback(tmp_path):
    """可读水位在文件写完推进，后续发布耗时不应混入磁盘写入指标。"""
    clock = [10.0]

    async def publish(_chunk):
        clock[0] += 5

    collector = Collector({"id": "task"}, tmp_path, connection_factory=lambda _: None, on_log=publish)
    collector.write_latency = WriteLatency(clock=lambda: clock[0])
    write = collector._writer.write_many

    async def slow_write(chunks):
        clock[0] += .15
        return await write(chunks)

    collector._writer.write_many = slow_write
    collector.write_latency.begin()
    clock[0] += .1
    await collector._flush([(b"logged\n", datetime.now(UTC))])
    assert collector.write_latency.snapshot()["p99Ms"] == pytest.approx(250)
    assert collector.write_latency.snapshot()["samples"] == 1
    await collector._writer.close()


async def test_failed_write_never_becomes_a_successful_latency_sample(tmp_path):
    clock = [0.0]
    collector = Collector({"id": "task"}, tmp_path, connection_factory=lambda _: None)
    collector.write_latency = WriteLatency(clock=lambda: clock[0])

    async def failed_write(_chunks):
        clock[0] = .3
        raise OSError("synthetic disk failure")

    collector._writer.write_many = failed_write
    with pytest.raises(OSError):
        await collector._flush([(b"logged\n", datetime.now(UTC))])
    assert collector.write_latency.snapshot()["samples"] == 0
    assert collector.write_latency.snapshot()["pendingMs"] == 300
