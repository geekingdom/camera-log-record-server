"""性能控制器运行时接线回归，验证热配置真正落到各受限执行器。"""

from types import SimpleNamespace

import pytest
from camera_logs.collection import buffering
from camera_logs.logs import write_io
from camera_logs.logs.archive_access import read_limiter
from camera_logs.node import performance_runtime

MIB = 1024 * 1024


def test_archive_snapshot_distinguishes_queue_and_processing(monkeypatch):
    """一个慢归档执行时，后续归档应明确显示排队而非把全部等待算作压缩耗时。"""
    tick = [10.0]
    monkeypatch.setattr(performance_runtime.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(performance_runtime, "_archives", {})
    first, second = object(), object()
    performance_runtime.archive_started(first)
    tick[0] = 20
    performance_runtime.archive_running(first)
    performance_runtime.archive_started(second)
    tick[0] = 320
    assert performance_runtime.archive_snapshot() == {
        "pendingJobs": 2, "waitingJobs": 1, "runningJobs": 1,
        "oldestSeconds": 310, "oldestWaitingSeconds": 300, "oldestRunningSeconds": 300,
    }
    performance_runtime.archive_finished(first)
    performance_runtime.archive_finished(second)
    assert performance_runtime.archive_snapshot()["pendingJobs"] == 0


@pytest.fixture(autouse=True)
def restore_runtime_globals():
    """运行时模块保存进程级预算，测试结束必须恢复避免污染后续采集回归。"""
    configuration = dict(buffering._configuration)
    write_limit, write_closed = write_io._limit, write_io._closed
    rate, next_at = read_limiter.rate, read_limiter.next_at
    effective, pressured, archives = dict(performance_runtime._effective), performance_runtime._pressured, dict(performance_runtime._archives)
    yield
    buffering.configure(configuration)
    write_io.configure(write_limit)
    write_io._closed = write_closed
    read_limiter.rate, read_limiter.next_at = rate, next_at
    performance_runtime._effective = effective
    performance_runtime._pressured = pressured
    performance_runtime._archives.clear(); performance_runtime._archives.update(archives)


def worker(telemetry, age=0):
    """构造不含数据库或真实宿主采样的 Worker 最小替身。"""
    return SimpleNamespace(telemetry=SimpleNamespace(value=telemetry, age_seconds=lambda: age))


def limits(*, available=1024 * MIB, cpu=4):
    """统一提供遥测中由采样器上报的资源限制结构。"""
    return {"effectiveCpu": cpu, "memoryLimitBytes": 2 * 1024 * MIB,
            "memoryAvailableBytes": available, "source": "TEST"}


@pytest.mark.asyncio
async def test_update_performance_applies_manual_budget_to_buffer_writer_and_reader():
    """手动策略同时热更新接收缓冲、写 I/O 门槛和共享归档/搜索读取限速。"""
    subject = worker({"cpuPercent": 20, "memoryPercent": 20, "resourceLimits": limits()})
    config = {"performance": {"mode": "MANUAL", "bufferMiB": 128, "writeConcurrency": 6,
                              "batchKiB": 512, "flushIntervalMs": 25, "readMiBPerSecond": 20},
              "writeLatencyLimitMs": 200}

    status = await performance_runtime.update_performance(
        subject, config, {"writeLatencyMs": 1}, 10
    )

    assert status["state"] == "MANUAL"
    assert status["admissionBlocked"] is False
    assert buffering._configuration == {"bufferMiB": 128, "batchKiB": 512, "flushIntervalMs": 25}
    assert write_io.snapshot()["concurrency"] == 6
    assert read_limiter.rate == 20 * MIB
    assert config["writeLatencyLimitMs"] == 200


@pytest.mark.asyncio
async def test_unknown_or_low_memory_is_not_admitted_and_uses_minimum_read_buffer():
    """遥测过期和已知低内存都拒绝新会话，且不把默认 64MiB 当作安全可用。"""
    unknown = await performance_runtime.update_performance(
        worker({"cpuPercent": 20, "memoryPercent": 20, "resourceLimits": limits()}, age=16), {},
        {"writeLatencyMs": 1}, 10,
    )
    assert unknown["admissionBlocked"] is True
    assert unknown["effective"]["bufferMiB"] == 2
    constrained = await performance_runtime.update_performance(
        worker({"cpuPercent": 20, "memoryPercent": 20, "resourceLimits": limits(available=MIB)}),
        {"performance": {"mode": "MANUAL", "bufferMiB": 64, "writeConcurrency": 4,
                         "batchKiB": 256, "flushIntervalMs": 100, "readMiBPerSecond": 50}},
        {"writeLatencyMs": 1}, 10,
    )
    assert constrained["state"] == "MANUAL"
    assert constrained["admissionBlocked"] is True
    assert constrained["effective"]["bufferMiB"] == 2


@pytest.mark.asyncio
async def test_runtime_auto_pressure_is_bounded_and_keeps_admin_latency_threshold():
    """持续 CPU 压力只在策略上下限内降级，管理员写延迟阈值不会被性能器修改。"""
    subject = worker({"cpuPercent": 95, "memoryPercent": 20, "resourceLimits": limits()})
    config = {"performance": {"mode": "AUTO", "bufferMiB": 64, "writeConcurrency": 4,
                              "batchKiB": 256, "flushIntervalMs": 100, "readMiBPerSecond": 50},
              "writeLatencyLimitMs": 333}
    for _ in range(3):
        status = await performance_runtime.update_performance(subject, config, {"writeLatencyMs": 100}, 20)
    assert status["state"] == "THROTTLED"
    assert 1 <= status["effective"]["writeConcurrency"] < 4
    assert 1 <= status["effective"]["readMiBPerSecond"] < 50
    assert status["effective"]["batchKiB"] == 256
    assert status["effective"]["flushIntervalMs"] == 100
    assert config["writeLatencyLimitMs"] == 333


async def test_batch_wait_does_not_throttle_background_when_actual_write_is_fast():
    """采集总延迟保留准入用途，AUTO降后台预算只看实际写入等待和执行耗时。"""
    subject = worker({"cpuPercent": 20, "memoryPercent": 20, "resourceLimits": limits()})
    for _ in range(4):
        status = await performance_runtime.update_performance(
            subject, {"writeLatencyLimitMs": 100}, {"writeLatencyMs": 150, "writeIoLatencyMs": 5}, 20)
    assert status["state"] == "NORMAL"
    assert status["effective"]["readMiBPerSecond"] == 50
