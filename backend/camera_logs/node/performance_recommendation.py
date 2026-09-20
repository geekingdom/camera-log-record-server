"""根据节点新鲜资源快照生成保守的可手动采纳性能建议。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime

from camera_logs.common.database import now
from camera_logs.node.health import fresh, measurements
from camera_logs.node.performance import PerformancePolicy
from camera_logs.node.resource_limits import ResourceLimits

MIB = 1024 * 1024
MIN_RECOMMENDABLE_MEMORY = 64 * MIB
LOW_MEMORY = 512 * MIB
HIGH_MEMORY = 4 * 1024 * MIB


def performance_recommendation(node: Mapping[str, object] | None, current: datetime | None = None) -> dict[str, object]:
    """只以新鲜心跳和遥测资源上限给建议；无法证明资源时不猜测可保存值。"""
    current = current or now()
    snapshot = node or {}
    reported_at = snapshot.get("heartbeat")
    if not fresh(reported_at, current, 30):
        return _unavailable("节点心跳已过期，无法推荐", reported_at)
    telemetry = measurements(snapshot, current)
    if not telemetry:
        return _unavailable("节点遥测已过期，无法推荐", reported_at)
    limits = _resource_limits(telemetry.get("resourceLimits"))
    if limits is None:
        return _unavailable("节点资源限制未知，无法推荐", reported_at)
    if limits.memoryAvailableBytes < MIN_RECOMMENDABLE_MEMORY:
        return _unavailable("可用内存不足64MiB，无法推荐", reported_at)
    policy = _policy(limits)
    return {
        "available": True,
        "reason": "基于新鲜节点资源限制的保守推荐",
        "policy": policy.model_dump(),
        "resources": limits.public(),
        "reportedAt": reported_at,
    }


def _resource_limits(value: object) -> ResourceLimits | None:
    """只接受 Worker 已公开的完整有限资源限制，避免把缺失字段放大为宿主容量。"""
    if not isinstance(value, Mapping):
        return None
    cpu = _positive_number(value.get("effectiveCpu"))
    available = _nonnegative_integer(value.get("memoryAvailableBytes"))
    limit = _nonnegative_integer(value.get("memoryLimitBytes"))
    source = value.get("source")
    if cpu is None or available is None or limit is None or not isinstance(source, str) or not source:
        return None
    return ResourceLimits(cpu, limit, available, source)


def _policy(limits: ResourceLimits) -> PerformancePolicy:
    """把实际 CPU 和可用内存映射到策略合法范围，不依赖负载百分比猜测容量。"""
    available = limits.memoryAvailableBytes
    low = limits.effectiveCpu <= 2 or available <= LOW_MEMORY
    high = limits.effectiveCpu >= 8 and available >= HIGH_MEMORY
    return PerformancePolicy(
        bufferMiB=min(256, max(16, available // MIB // 16)),
        writeConcurrency=min(8, max(1, math.floor(limits.effectiveCpu))),
        batchKiB=64 if low else 256,
        flushIntervalMs=100,
        readMiBPerSecond=10 if low else 50 if high else 25,
    )


def _unavailable(reason: str, reported_at: object) -> dict[str, object]:
    """统一不可用输出，避免前端将缺失建议误解为默认策略。"""
    return {"available": False, "reason": reason, "policy": None, "resources": None, "reportedAt": reported_at}


def _positive_number(value: object) -> float | None:
    """仅接受有限正 CPU 核数，拒绝布尔与字符串。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _nonnegative_integer(value: object) -> int | None:
    """资源字节数必须是有限非负整数，避免浮点或字符串产生不明确的边界。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
