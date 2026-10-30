"""逐节点性能预算状态机，只给出建议值，不直接控制采集器。"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from camera_logs.node.resource_limits import ResourceLimits

MIB = 1024 * 1024
PRESSURE_SAMPLES = 3
COOLDOWN_SECONDS = 30
RECOVERY_SECONDS = 60


class PerformancePolicy(BaseModel):
    """管理员可保存的性能预算上限，拒绝未知字段以免拼写错误扩大资源消耗。"""

    model_config = ConfigDict(extra="forbid")

    mode: str = Field("AUTO", pattern="^(AUTO|MANUAL)$", description='节点性能策略模式；AUTO 在资源预算上限内自适应，MANUAL 使用明确参数。')
    bufferMiB: Annotated[int, Field(strict=True, ge=16, le=4096)] = Field(default=64, description='节点日志写入缓冲预算，单位MiB，整数16至4096，默认64。')
    writeConcurrency: Annotated[int, Field(strict=True, ge=1, le=32)] = Field(default=4, description='节点允许并发执行的写入工作数，整数1至32，默认4。')
    batchKiB: Annotated[int, Field(strict=True, ge=16, le=1024)] = Field(default=256, description='节点单次写入批量预算，单位KiB，整数16至1024，默认256。')
    flushIntervalMs: Annotated[int, Field(strict=True, ge=10, le=200)] = Field(default=100, description='节点写入批次最长等待，单位毫秒，整数10至200，默认100。')
    readMiBPerSecond: Annotated[int, Field(strict=True, ge=1, le=200)] = Field(default=50, description='节点逻辑读取预算，单位MiB/s，整数1至200，默认50；后台归档占80%、索引占20%，搜索与快照使用独立的同值限速器；不是采集输入上限或跨进程全盘总带宽上限。')


class PerformanceController:
    """根据持续压力缓慢下调 AUTO 预算；MANUAL 仅受不可突破的资源安全上限约束。"""

    def __init__(self) -> None:
        self.effective: dict[str, int] | None = None
        self.state = "NORMAL"
        self.reason = "初始预算"
        self.changed_at: datetime | None = None
        self.pressure_samples = 0
        self.stable_since: float | None = None
        self.last_adjusted: float | None = None
        self.mode: str | None = None
        self.baseline: dict[str, int] | None = None
        self.pressure_active = False

    def evaluate(
        self,
        policy: PerformancePolicy,
        telemetry: Mapping[str, object] | None,
        write_latency: Mapping[str, object] | None,
        limits: ResourceLimits | Mapping[str, object],
        disk_percent: object,
        clock: Callable[[], datetime | float] = time.monotonic,
    ) -> dict[str, object]:
        """返回当前有效预算及状态；同一状态的普通采样不会反复刷新 ``changedAt``。"""
        moment, tick = _moment(clock)
        resource_limits = _limits(limits)
        baseline, safety_reason, admission_blocked = _safe_budget(policy, resource_limits)
        pressure_reason = _pressure_reason(telemetry, write_latency)
        self.pressure_active = pressure_reason is not None
        changed_baseline = self.baseline != baseline
        if self.effective is None or self.mode != policy.mode:
            self.effective = baseline
            self.state = "MANUAL" if policy.mode == "MANUAL" else "NORMAL"
            self.reason = safety_reason or ("手动预算" if policy.mode == "MANUAL" else "初始预算")
            self.changed_at = moment
            self.mode = policy.mode
            self.pressure_samples = 0
            self.stable_since = None
            self.last_adjusted = None
            self.baseline = baseline
        if policy.mode == "MANUAL":
            self.effective = baseline
            self.state = "MANUAL"
            self.reason = safety_reason or "手动预算"
            if changed_baseline:
                self.changed_at = moment
            self.baseline = baseline
            return self._result(resource_limits, admission_blocked, tick)
        if changed_baseline:
            self.effective = baseline if self.state == "NORMAL" else _bounded(self.effective, baseline)
            self.reason = safety_reason or "策略或资源预算已更新"
            self.changed_at = moment
            self.baseline = baseline
        else:
            self.effective = _bounded(self.effective, baseline)
        if self.effective == baseline and self.state == "THROTTLED":
            self.state, self.reason, self.changed_at = "NORMAL", safety_reason or "当前预算已满足", moment
        elif safety_reason and self.state == "NORMAL" and self.reason != safety_reason:
            self.reason, self.changed_at = safety_reason, moment
        if pressure_reason:
            self.pressure_samples += 1
            self.stable_since = None
            if self.pressure_samples >= PRESSURE_SAMPLES:
                if self.state != "THROTTLED" or self.reason != pressure_reason:
                    self.state, self.reason, self.changed_at = "THROTTLED", pressure_reason, moment
                if not self._adjustable(tick):
                    return self._result(resource_limits, admission_blocked, tick)
                lowered = dict(self.effective)
                lowered["readMiBPerSecond"] = max(1, lowered["readMiBPerSecond"] // 2)
                if _compute_pressure(telemetry):
                    lowered["writeConcurrency"] = max(1, lowered["writeConcurrency"] - 1)
                if lowered != self.effective:
                    self.effective = lowered
                    self.changed_at, self.last_adjusted = moment, tick
                # 即使预算已经达到最小值，也必须记录稳定窗口，避免永久停留在
                # THROTTLED；后续健康采样仍需经过完整恢复窗口再结束状态。
                self.stable_since = tick
            return self._result(resource_limits, admission_blocked, tick)
        self.pressure_samples = 0
        if not _telemetry_known(telemetry):
            self.stable_since = None
            return self._result(resource_limits, admission_blocked, tick)
        self.stable_since = self.stable_since or tick
        if (self.state in {"THROTTLED", "RECOVERING"} and self._adjustable(tick)
                and self.stable_since is not None and tick - self.stable_since >= RECOVERY_SECONDS):
            raised = {
                name: _recover_step(self.effective[name], baseline[name])
                if name in {"readMiBPerSecond", "writeConcurrency"} else self.effective[name]
                for name in baseline
            }
            if raised != self.effective:
                self.effective, self.state, self.reason = raised, "RECOVERING", "持续稳定后小步恢复"
                self.changed_at, self.last_adjusted, self.stable_since = moment, tick, tick
                if raised == baseline:
                    self.state, self.reason = "NORMAL", "已恢复至当前预算"
            elif self.effective == baseline and self.state == "THROTTLED":
                self.state, self.reason, self.changed_at = "NORMAL", "已恢复至当前预算", moment
        return self._result(resource_limits, admission_blocked, tick)

    def _adjustable(self, tick: float) -> bool:
        """每次自动调整至少间隔 30 秒，防止高频遥测产生来回震荡。"""
        return self.last_adjusted is None or tick - self.last_adjusted >= COOLDOWN_SECONDS

    def _result(self, limits: ResourceLimits, admission_blocked: bool, tick: float) -> dict[str, object]:
        """输出保持 API 可序列化的扁平结构，effective 不包含仅管理层需要的 mode。"""
        stable_seconds = 0
        if not self.pressure_active and self.stable_since is not None:
            stable_seconds = max(0, int(tick - self.stable_since))
        return {
            "effective": dict(self.effective or {}), "state": self.state, "reason": self.reason,
            "changedAt": self.changed_at, "resources": limits.public(), "admissionBlocked": admission_blocked,
            "baseline": dict(self.baseline or {}),
            "budgetReduced": bool(self.baseline and self.effective != self.baseline),
            "pressureActive": self.pressure_active,
            "pressureSamples": self.pressure_samples,
            "recoveryStableSeconds": stable_seconds,
        }


def _safe_budget(policy: PerformancePolicy, limits: ResourceLimits) -> tuple[dict[str, int], str | None, bool]:
    """在策略上限内应用 CPU 与可用内存硬边界，绝不把未知资源解释为可无限扩张。"""
    budget = policy.model_dump(exclude={"mode"})
    cpu_cap = max(1, int(limits.effectiveCpu * 2))
    budget["writeConcurrency"] = min(budget["writeConcurrency"], cpu_cap)
    available = limits.memoryAvailableBytes
    if available <= 0:
        budget["bufferMiB"] = 2
        return budget, "内存未知，拒绝新准入", True
    memory_cap = max(2, available // MIB // 4)
    constrained = budget["bufferMiB"] > memory_cap
    budget["bufferMiB"] = min(budget["bufferMiB"], memory_cap)
    return budget, "内存安全上限" if constrained else None, available < 2 * MIB


def _bounded(current: Mapping[str, int], ceiling: Mapping[str, int]) -> dict[str, int]:
    """配置或实际资源变小时立即收紧，自动恢复永远不超过最新管理上限。"""
    return {
        name: value if name in {"bufferMiB", "batchKiB", "flushIntervalMs"}
        else min(int(current.get(name, value)), value)
        for name, value in ceiling.items()
    }


def _pressure_reason(telemetry: Mapping[str, object] | None, write_latency: Mapping[str, object] | None) -> str | None:
    """仅 CPU、内存和写入延迟触发降级；磁盘容量仍由 Worker 独立拒绝新准入。"""
    values = telemetry or {}
    reasons = []
    if _at_least(values.get("cpuPercent"), 90):
        reasons.append("CPU")
    if _at_least(values.get("memoryPercent"), 90):
        reasons.append("内存")
    if _greater((write_latency or {}).get("writeLatencyMs"), (write_latency or {}).get("limitMs")):
        reasons.append("写入延迟")
    return "持续" + "、".join(reasons) + "压力" if reasons else None


def _recover_step(current: int, ceiling: int) -> int:
    """每次稳定恢复至少一步并收回一半差额，始终不超过最新管理员或资源上限。"""
    remaining = max(0, ceiling - current)
    return min(ceiling, current + max(1, math.ceil(remaining / 2))) if remaining else current


def _compute_pressure(telemetry: Mapping[str, object] | None) -> bool:
    """只有 CPU 或内存持续高压才减少写线程并发，避免写延迟排队形成恶性反馈。"""
    values = telemetry or {}
    return _at_least(values.get("cpuPercent"), 90) or _at_least(values.get("memoryPercent"), 90)


def _telemetry_known(telemetry: Mapping[str, object] | None) -> bool:
    """CPU 和内存必须都有效才允许恢复，任一缺失都不能据此扩大后台预算。"""
    values = telemetry or {}
    return _number(values.get("cpuPercent")) is not None and _number(values.get("memoryPercent")) is not None


def _at_least(value: object, threshold: float) -> bool:
    """拒绝 bool、NaN、无穷和负数，未知遥测不应制造虚假的压力事件。"""
    parsed = _number(value)
    return parsed is not None and parsed >= threshold


def _greater(value: object, limit: object) -> bool:
    """必须同时有可信延迟和外部提供的管理阈值，才允许触发自适应降级。"""
    left, right = _number(value), _number(limit)
    return left is not None and right is not None and left > right


def _number(value: object) -> float | None:
    """统一验证外部数值输入，防止非法遥测导致错误资源限制。"""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _moment(clock: Callable[[], datetime | float]) -> tuple[datetime, float]:
    """默认采用单调时钟计算冷却，测试传 UTC 时间时也产生一致的稳定秒数。"""
    value = clock()
    if isinstance(value, datetime):
        moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return moment, moment.timestamp()
    return datetime.now(UTC), float(value)


def _limits(value: ResourceLimits | Mapping[str, object]) -> ResourceLimits:
    """运行时遥测使用字典，纯逻辑调用可直接传不可变资源快照。"""
    if isinstance(value, ResourceLimits):
        return value
    return ResourceLimits(
        _positive_float(value.get("effectiveCpu"), 1.0),
        _positive_int(value.get("memoryLimitBytes")),
        _positive_int(value.get("memoryAvailableBytes")),
        str(value.get("source") or "UNKNOWN"),
    )


def _positive_float(value: object, fallback: float) -> float:
    """映射中的非法 CPU 值回退单核，避免运行时外部数据抬高并发。"""
    parsed = _number(value)
    return parsed if parsed is not None and parsed > 0 else fallback


def _positive_int(value: object) -> int:
    """内存字节数只接受非负有限整数，未知值按零传递给安全预算。"""
    parsed = _number(value)
    return int(parsed) if parsed is not None else 0
