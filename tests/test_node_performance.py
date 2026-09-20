"""逐节点自适应性能预算的纯逻辑回归，不读取执行机真实资源。"""

from datetime import UTC, datetime, timedelta

import pytest
from camera_logs.node.performance import PerformanceController, PerformancePolicy
from camera_logs.node.resource_limits import ResourceLimits
from pydantic import ValidationError

MIB = 1024 * 1024
START = datetime(2026, 9, 20, tzinfo=UTC)


def limits(*, cpu=4.0, available=2_048 * MIB, limit=4_096 * MIB, source="TEST"):
    """构造确定资源快照，避免测试依赖宿主机 Docker 或 cgroup 配置。"""
    return ResourceLimits(cpu, limit, available, source)


def telemetry(**changes):
    """给出低负载遥测基线，单个测试仅覆盖需要的压力指标。"""
    return {"cpuPercent": 20, "memoryPercent": 25} | changes


def test_policy_rejects_unknown_fields_and_enforces_documented_budgets():
    """管理配置拒绝拼写错误和超出硬边界的值，不能静默扩大节点预算。"""
    assert PerformancePolicy().model_dump() == {
        "mode": "AUTO", "bufferMiB": 64, "writeConcurrency": 4, "batchKiB": 256,
        "flushIntervalMs": 100, "readMiBPerSecond": 50,
    }
    with pytest.raises(ValidationError):
        PerformancePolicy(bufferMiB=15)
    with pytest.raises(ValidationError):
        PerformancePolicy(unknownBudget=1)
    with pytest.raises(ValidationError):
        PerformancePolicy(bufferMiB=True)
    with pytest.raises(ValidationError):
        PerformancePolicy(readMiBPerSecond="50")


def test_resource_limits_detects_v2_quota_memory_and_affinity(tmp_path):
    """cgroup v2 配额和当前内存优先于宿主容量，并和 affinity 取更小 CPU。"""
    (tmp_path / "cpu.max").write_text("250000 100000\n")
    (tmp_path / "memory.max").write_text(str(1024 * MIB))
    (tmp_path / "memory.current").write_text(str(256 * MIB))

    found = ResourceLimits.detect(tmp_path, cpu_count=lambda: 8, affinity=lambda: {1, 2, 3},
                                  virtual_memory=lambda: (16 * MIB, 8 * MIB))

    assert found == ResourceLimits(2.5, 1024 * MIB, 8 * MIB, "CGROUP_V2")


def test_resource_limits_detects_v1_and_uses_conservative_unknown_values(tmp_path):
    """v1 无限制标记退回可用内存；完全未知时不给虚假的高资源预算。"""
    cpu = tmp_path / "cpu"; memory = tmp_path / "memory"
    cpu.mkdir(); memory.mkdir()
    (cpu / "cpu.cfs_quota_us").write_text("100000")
    (cpu / "cpu.cfs_period_us").write_text("100000")
    (memory / "memory.limit_in_bytes").write_text(str(512 * MIB))
    (memory / "memory.usage_in_bytes").write_text(str(128 * MIB))
    assert ResourceLimits.detect(tmp_path, cpu_count=lambda: 4, affinity=lambda: None,
                                 virtual_memory=lambda: (8 * MIB, 6 * MIB)) == ResourceLimits(
        1.0, 512 * MIB, 6 * MIB, "CGROUP_V1"
    )
    assert ResourceLimits.detect(tmp_path / "missing", cpu_count=lambda: None, affinity=lambda: None,
                                 virtual_memory=lambda: None) == ResourceLimits(1.0, 0, 0, "UNKNOWN")


def test_resource_limits_follows_process_cgroup_child_path(tmp_path):
    """统一层级挂载根不等于进程所在子组时，必须读取进程自身而非根组配额。"""
    child = tmp_path / "service" / "camera"; child.mkdir(parents=True)
    (child / "cpu.max").write_text("50000 100000")
    (child / "memory.max").write_text(str(128 * MIB))
    (child / "memory.current").write_text(str(64 * MIB))
    process = tmp_path / "self.cgroup"; process.write_text("0::/service/camera\n")
    assert ResourceLimits.detect(tmp_path, cpu_count=lambda: 8, affinity=lambda: None,
                                 virtual_memory=lambda: (8 * MIB, 96 * MIB), process_cgroup=process) == ResourceLimits(
        0.5, 128 * MIB, 64 * MIB, "CGROUP_V2"
    )


def test_resource_limits_uses_the_most_restrictive_cgroup_ancestor(tmp_path):
    """父组配额和内存余额比叶组更小时，进程必须继承父组限制。"""
    child = tmp_path / "service" / "camera"; child.mkdir(parents=True)
    (tmp_path / "cpu.max").write_text("200000 100000")
    (child.parent / "cpu.max").write_text("50000 100000")
    (tmp_path / "memory.max").write_text(str(512 * MIB))
    (tmp_path / "memory.current").write_text(str(128 * MIB))
    (child / "memory.max").write_text(str(256 * MIB))
    (child / "memory.current").write_text(str(32 * MIB))
    process = tmp_path / "self.cgroup"; process.write_text("0::/service/camera\n")

    found = ResourceLimits.detect(tmp_path, cpu_count=lambda: 8, affinity=lambda: None,
                                  virtual_memory=lambda: (8 * MIB, 512 * MIB), process_cgroup=process)

    assert found == ResourceLimits(0.5, 256 * MIB, 224 * MIB, "CGROUP_V2")


def test_resource_limits_uses_v1_controller_ancestors(tmp_path):
    """v1 的复合 CPU controller 与独立内存 controller 都继承父组限制。"""
    cpu = tmp_path / "cpu,cpuacct"; memory = tmp_path / "memory"
    cpu_child = cpu / "service" / "camera"; memory_child = memory / "service" / "camera"
    cpu_child.mkdir(parents=True); memory_child.mkdir(parents=True)
    (cpu / "cpu.cfs_quota_us").write_text("200000")
    (cpu / "cpu.cfs_period_us").write_text("100000")
    (cpu_child.parent / "cpu.cfs_quota_us").write_text("50000")
    (cpu_child.parent / "cpu.cfs_period_us").write_text("100000")
    (memory / "memory.limit_in_bytes").write_text(str(512 * MIB))
    (memory / "memory.usage_in_bytes").write_text(str(128 * MIB))
    (memory_child / "memory.limit_in_bytes").write_text(str(256 * MIB))
    (memory_child / "memory.usage_in_bytes").write_text(str(32 * MIB))
    process = tmp_path / "self.cgroup"
    process.write_text("2:cpu,cpuacct:/service/camera\n3:memory:/service/camera\n")

    found = ResourceLimits.detect(tmp_path, cpu_count=lambda: 8, affinity=lambda: None,
                                  virtual_memory=lambda: (8 * MIB, 512 * MIB), process_cgroup=process)

    assert found == ResourceLimits(0.5, 256 * MIB, 224 * MIB, "CGROUP_V1")


def test_resource_limits_treats_missing_cgroup_memory_current_as_unavailable(tmp_path):
    """有限内存上限缺少当前用量时不能假定尚有可分配内存。"""
    (tmp_path / "cpu.max").write_text("100000 100000")
    (tmp_path / "memory.max").write_text(str(128 * MIB))

    found = ResourceLimits.detect(tmp_path, cpu_count=lambda: 8, affinity=lambda: None,
                                  virtual_memory=lambda: (8 * MIB, 512 * MIB))

    assert found == ResourceLimits(1.0, 128 * MIB, 0, "CGROUP_V2")


def test_auto_reduces_only_after_three_pressure_samples_and_respects_cooldown():
    """短暂尖峰不抖动；持续写压才降低后台读取和并发，30 秒内不重复调节。"""
    controller = PerformanceController()
    policy = PerformancePolicy()
    normal = controller.evaluate(policy, telemetry(), {"writeLatencyMs": 10, "limitMs": 200},
                                 limits=limits(), disk_percent=20, clock=lambda: START)
    assert normal["effective"]["readMiBPerSecond"] == 50
    for offset in (1, 2):
        result = controller.evaluate(policy, telemetry(cpuPercent=95), {"writeLatencyMs": 300, "limitMs": 200},
                                     limits=limits(), disk_percent=20, clock=lambda offset=offset: START + timedelta(seconds=offset))
        assert result["state"] == "NORMAL"
    reduced = controller.evaluate(policy, telemetry(cpuPercent=95), {"writeLatencyMs": 300, "limitMs": 200},
                                  limits=limits(), disk_percent=20, clock=lambda: START + timedelta(seconds=3))
    assert reduced["state"] == "THROTTLED"
    assert reduced["effective"]["readMiBPerSecond"] == 25
    assert reduced["effective"]["writeConcurrency"] == 3
    held = controller.evaluate(policy, telemetry(cpuPercent=95), {"writeLatencyMs": 300, "limitMs": 200},
                               limits=limits(), disk_percent=20, clock=lambda: START + timedelta(seconds=10))
    assert held["effective"] == reduced["effective"]


def test_write_latency_pressure_only_reduces_background_read_not_write_concurrency():
    """写入延迟可能来自排队，单指标压力不能继续压低写线程并发而放大积压。"""
    controller = PerformanceController()
    policy = PerformancePolicy(readMiBPerSecond=16, writeConcurrency=4)
    for second in (0, 1, 2):
        result = controller.evaluate(policy, telemetry(), {"writeLatencyMs": 300, "limitMs": 200}, limits=limits(),
                                     disk_percent=20, clock=lambda second=second: START + timedelta(seconds=second))
    assert result["state"] == "THROTTLED"
    assert result["effective"]["readMiBPerSecond"] == 8
    assert result["effective"]["writeConcurrency"] == 4
    assert result["effective"]["batchKiB"] == policy.batchKiB
    assert result["effective"]["flushIntervalMs"] == policy.flushIntervalMs


def test_auto_recovers_a_bounded_proportional_step_after_sixty_seconds_of_stable_samples():
    """恢复须持续 60 秒；每阶收回一半差额，避免低速预算长期压缩积压。"""
    controller = PerformanceController()
    policy = PerformancePolicy(readMiBPerSecond=8, writeConcurrency=4)
    for seconds in (0, 1, 2):
        reduced = controller.evaluate(policy, telemetry(memoryPercent=95), {"writeLatencyMs": 1, "limitMs": 200},
                                      limits=limits(), disk_percent=20,
                                      clock=lambda seconds=seconds: START + timedelta(seconds=seconds))
    assert reduced["effective"]["readMiBPerSecond"] == 4
    stable = controller.evaluate(policy, telemetry(), {"writeLatencyMs": 1, "limitMs": 200}, limits=limits(),
                                 disk_percent=20, clock=lambda: START + timedelta(seconds=61))
    assert stable["effective"]["readMiBPerSecond"] == 4
    restored = controller.evaluate(policy, telemetry(), {"writeLatencyMs": 1, "limitMs": 200}, limits=limits(),
                                   disk_percent=20, clock=lambda: START + timedelta(seconds=63))
    assert restored["state"] == "RECOVERING"
    assert restored["effective"]["readMiBPerSecond"] == 6
    assert restored["effective"]["writeConcurrency"] == 4


def test_disk_capacity_does_not_throttle_background_archive_reads_or_hide_pressure_reason():
    """磁盘容量仍由 Worker 拒绝新准入，不能被误作 I/O 压力而降低归档读取预算。"""
    controller = PerformanceController()
    policy = PerformancePolicy(readMiBPerSecond=50)
    for second in (0, 1, 2):
        result = controller.evaluate(policy, telemetry(), {"writeLatencyMs": 1, "limitMs": 200},
                                     limits=limits(), disk_percent=99,
                                     clock=lambda second=second: START + timedelta(seconds=second))
    assert result["state"] == "NORMAL"
    assert result["effective"]["readMiBPerSecond"] == 50


@pytest.mark.parametrize(("sample", "latency", "reason"), [
    (telemetry(cpuPercent=95), {"writeLatencyMs": 1, "limitMs": 200}, "持续CPU压力"),
    (telemetry(memoryPercent=95), {"writeLatencyMs": 1, "limitMs": 200}, "持续内存压力"),
    (telemetry(), {"writeLatencyMs": 201, "limitMs": 200}, "持续写入延迟压力"),
])
def test_auto_throttle_reason_identifies_the_pressure_source(sample, latency, reason):
    """节点状态必须区分资源、内存和写延迟压力，方便排查预算变化原因。"""
    controller = PerformanceController()
    for second in (0, 1, 2):
        result = controller.evaluate(PerformancePolicy(), sample, latency, limits=limits(), disk_percent=20,
                                     clock=lambda second=second: START + timedelta(seconds=second))
    assert result["state"] == "THROTTLED"
    assert result["reason"] == reason


def test_auto_updates_pressure_reason_after_read_budget_reaches_its_minimum():
    """读预算已到下限时仍显示当前压力类别，避免状态页保留过期原因。"""
    controller = PerformanceController()
    policy = PerformancePolicy(readMiBPerSecond=1)
    for second in (0, 1, 2):
        result = controller.evaluate(policy, telemetry(), {"writeLatencyMs": 201, "limitMs": 200},
                                     limits=limits(), disk_percent=20,
                                     clock=lambda second=second: START + timedelta(seconds=second))
    assert result["state"] == "THROTTLED"
    assert result["reason"] == "持续写入延迟压力"
    for second in (3, 4, 5):
        result = controller.evaluate(policy, telemetry(cpuPercent=95), {"writeLatencyMs": 1, "limitMs": 200},
                                     limits=limits(), disk_percent=20,
                                     clock=lambda second=second: START + timedelta(seconds=second))
    assert result["state"] == "THROTTLED"
    assert result["reason"] == "持续CPU压力"


def test_manual_never_adapts_but_hard_cpu_and_memory_limits_constrain_it():
    """手动预算不因遥测自动调节，但 128MiB 可用内存和单核不得允许过大缓冲或并发。"""
    controller = PerformanceController()
    policy = PerformancePolicy(mode="MANUAL", bufferMiB=512, writeConcurrency=32, readMiBPerSecond=200)
    first = controller.evaluate(policy, telemetry(cpuPercent=99), {"writeLatencyMs": 500, "limitMs": 200},
                                limits=limits(cpu=1, available=128 * MIB, limit=256 * MIB), disk_percent=95,
                                clock=lambda: START)
    later = controller.evaluate(policy, telemetry(cpuPercent=99), {"writeLatencyMs": 500, "limitMs": 200},
                                limits=limits(cpu=1, available=128 * MIB, limit=256 * MIB), disk_percent=95,
                                clock=lambda: START + timedelta(minutes=2))
    assert first["state"] == later["state"] == "MANUAL"
    assert first["effective"] == later["effective"]
    assert first["effective"]["bufferMiB"] == 32
    assert first["effective"]["writeConcurrency"] == 2


def test_cpu_quota_survives_unlimited_memory_and_low_memory_blocks_new_admission(tmp_path):
    """CPU 与内存 cgroup 限制独立；低于协议最小 2MiB 缓冲时明确拒绝新准入。"""
    (tmp_path / "cpu.max").write_text("100000 100000")
    (tmp_path / "memory.max").write_text("max")
    found = ResourceLimits.detect(tmp_path, cpu_count=lambda: 8, affinity=lambda: None,
                                  virtual_memory=lambda: (8 * MIB, 6 * MIB))
    assert found == ResourceLimits(1.0, 0, 6 * MIB, "CGROUP_V2")
    result = PerformanceController().evaluate(
        PerformancePolicy(bufferMiB=64), telemetry(), {"writeLatencyMs": 1, "limitMs": 200},
        limits=limits(available=MIB), disk_percent=20, clock=lambda: START,
    )
    assert result["effective"]["bufferMiB"] == 2
    assert result["admissionBlocked"] is True


def test_unknown_telemetry_never_counts_as_stable_recovery():
    """丢失 CPU/内存遥测时保持已降级预算，不能用空字典恢复后台吞吐。"""
    controller = PerformanceController()
    policy = PerformancePolicy(readMiBPerSecond=8)
    for second in (0, 1, 2):
        controller.evaluate(policy, telemetry(cpuPercent=95), {"writeLatencyMs": 1, "limitMs": 200}, limits=limits(),
                            disk_percent=20, clock=lambda second=second: START + timedelta(seconds=second))
    unknown = controller.evaluate(policy, {"cpuPercent": 20, "memoryPercent": None}, {"limitMs": 200},
                                  limits=limits(), disk_percent=20, clock=lambda: START + timedelta(minutes=5))
    assert unknown["state"] == "THROTTLED"
    assert unknown["effective"]["readMiBPerSecond"] == 4


def test_policy_or_resource_change_refreshes_manual_and_normal_effective_budget():
    """手动改配置与 NORMAL 状态下的扩容必须立即显示，而非永远卡在旧较小值。"""
    controller = PerformanceController()
    first = controller.evaluate(PerformancePolicy(mode="MANUAL", bufferMiB=64), telemetry(), {"limitMs": 200},
                                limits=limits(), disk_percent=20, clock=lambda: START)
    changed = controller.evaluate(PerformancePolicy(mode="MANUAL", bufferMiB=128), telemetry(), {"limitMs": 200},
                                  limits=limits(), disk_percent=20, clock=lambda: START + timedelta(seconds=1))
    assert changed["effective"]["bufferMiB"] == 128
    assert changed["changedAt"] > first["changedAt"]
    auto = PerformanceController()
    auto.evaluate(PerformancePolicy(readMiBPerSecond=10, writeConcurrency=2), telemetry(), {"limitMs": 200},
                  limits=limits(cpu=1), disk_percent=20, clock=lambda: START)
    expanded = auto.evaluate(PerformancePolicy(readMiBPerSecond=20, writeConcurrency=4), telemetry(), {"limitMs": 200},
                             limits=limits(cpu=4), disk_percent=20, clock=lambda: START + timedelta(seconds=1))
    assert expanded["effective"]["readMiBPerSecond"] == 20
    assert expanded["effective"]["writeConcurrency"] == 4


def test_unknown_sample_resets_recovery_window_and_safety_reason_refreshes():
    """空遥测后须重新累计 60 秒稳定期；内存未知要刷新状态理由并阻断准入。"""
    controller = PerformanceController()
    policy = PerformancePolicy(readMiBPerSecond=8)
    for second in (0, 1, 2):
        controller.evaluate(policy, telemetry(cpuPercent=95), {"limitMs": 200}, limits=limits(), disk_percent=20,
                            clock=lambda second=second: START + timedelta(seconds=second))
    controller.evaluate(policy, telemetry(), {"limitMs": 200}, limits=limits(), disk_percent=20,
                        clock=lambda: START + timedelta(seconds=10))
    controller.evaluate(policy, {"cpuPercent": None, "memoryPercent": None}, {"limitMs": 200}, limits=limits(),
                        disk_percent=20, clock=lambda: START + timedelta(seconds=20))
    held = controller.evaluate(policy, telemetry(), {"limitMs": 200}, limits=limits(), disk_percent=20,
                               clock=lambda: START + timedelta(seconds=70))
    assert held["state"] == "THROTTLED"
    constrained = controller.evaluate(policy, telemetry(), {"limitMs": 200}, limits=limits(available=0), disk_percent=20,
                                      clock=lambda: START + timedelta(seconds=71))
    assert constrained["reason"] == "内存未知，拒绝新准入"
    assert constrained["admissionBlocked"] is True
