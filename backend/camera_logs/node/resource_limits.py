"""读取节点可用 CPU、内存硬限制，为性能预算提供保守上界。"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil


@dataclass(frozen=True)
class ResourceLimits:
    """一次资源探测结果；零内存表示无法可靠探测，调用方必须采取保守策略。"""

    effectiveCpu: float
    memoryLimitBytes: int
    memoryAvailableBytes: int
    source: str

    @classmethod
    def detect(
        cls,
        root: Path = Path("/sys/fs/cgroup"),
        *,
        cpu_count: Callable[[], int | None] = os.cpu_count,
        affinity: Callable[[], set[int] | None] | None = None,
        virtual_memory: Callable[[], object | tuple[int, int] | None] = psutil.virtual_memory,
        process_cgroup: Path = Path("/proc/self/cgroup"),
    ) -> ResourceLimits:
        """优先读取 cgroup v2、其次 v1；读取失败不臆测宿主机的大容量。"""
        host_cpu = _positive(cpu_count())
        affinity_cpu = _affinity_count(affinity)
        candidates = [value for value in (host_cpu, affinity_cpu) if value is not None]
        fallback_cpu = min(candidates) if candidates else 1.0
        available = _available_memory(virtual_memory)
        v2_cpu, v2_memory = _read_v2(root, process_cgroup)
        if v2_cpu is not None or v2_memory is not None:
            return cls(
                min(fallback_cpu, v2_cpu or fallback_cpu),
                v2_memory[0] if v2_memory else 0,
                _available(available, v2_memory),
                "CGROUP_V2",
            )
        v1_cpu, v1_memory = _read_v1(root, process_cgroup)
        if v1_cpu is not None or v1_memory is not None:
            return cls(
                min(fallback_cpu, v1_cpu or fallback_cpu),
                v1_memory[0] if v1_memory else 0,
                _available(available, v1_memory),
                "CGROUP_V1",
            )
        if candidates and available is not None:
            return cls(fallback_cpu, 0, available, "HOST")
        return cls(1.0, 0, 0, "UNKNOWN")

    def public(self) -> dict[str, float | int | str]:
        """转换为心跳或控制器可直接序列化的稳定字段名。"""
        return {
            "effectiveCpu": self.effectiveCpu,
            "memoryLimitBytes": self.memoryLimitBytes,
            "memoryAvailableBytes": self.memoryAvailableBytes,
            "source": self.source,
        }


def detect_resources() -> ResourceLimits:
    """供遥测采样线程调用的默认资源探测入口，避免运行时依赖测试注入细节。"""
    return ResourceLimits.detect()


def _read_text(path: Path) -> str | None:
    """限制探测失败为未知，不把权限或容器挂载错误升级为节点故障。"""
    try:
        return path.read_text().strip()
    except (OSError, TypeError):
        return None


def _positive(value: object) -> float | None:
    """仅接受有限正数，拒绝布尔值和 cgroup 的无上限哨兵。"""
    if isinstance(value, bool):
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) and converted > 0 else None


def _affinity_count(affinity: Callable[[], set[int] | None] | None) -> float | None:
    """Linux affinity 不可读取时保留其它来源，避免平台差异导致探测失败。"""
    if affinity is None:
        getter = getattr(os, "sched_getaffinity", None)
        affinity = (lambda: getter(0)) if getter is not None else lambda: None
    try:
        values = affinity()
    except OSError:
        return None
    return _positive(len(values)) if values is not None else None


def _available_memory(reader: Callable[[], object | tuple[int, int] | None]) -> int | None:
    """兼容 psutil 对象与测试传入的 ``(total, available)`` 小型替身。"""
    try:
        value = reader()
    except OSError:
        return None
    attribute = getattr(value, "available", None)
    value = attribute if attribute is not None else value[1] if isinstance(value, tuple) and len(value) >= 2 else None
    parsed = _positive(value)
    return int(parsed) if parsed is not None else None


def _read_v2(root: Path, process_cgroup: Path) -> tuple[float | None, tuple[int, int] | None]:
    """v2 沿进程所属层级汇总限制，父组限制同样约束当前进程。"""
    paths = _visible_paths(root, _member_path(root, process_cgroup, None))
    cpu_limits = [_quota_limit(_read_text(path / "cpu.max")) for path in paths]
    cpu = min((value for value in cpu_limits if value is not None), default=None)
    memory = _memory_levels(paths, "memory.max", "memory.current")
    return cpu, memory


def _read_v1(root: Path, process_cgroup: Path) -> tuple[float | None, tuple[int, int] | None]:
    """v1 的各 controller 挂载点分别沿父子层级计算有效限制。"""
    cpu_mount = next((root / name for name in ("cpu", "cpu,cpuacct") if (root / name).exists()), None)
    if cpu_mount is not None:
        cpu_paths = _visible_paths(cpu_mount, _member_path(cpu_mount, process_cgroup, cpu_mount.name))
        cpu_limits = [
            _quota_pair_limit(_read_text(path / "cpu.cfs_quota_us"), _read_text(path / "cpu.cfs_period_us"))
            for path in cpu_paths
        ]
        cpu = min((value for value in cpu_limits if value is not None), default=None)
    else:
        cpu = None
    memory_mount = root / "memory"
    memory_paths = _visible_paths(memory_mount, _member_path(memory_mount, process_cgroup, "memory"))
    memory = _memory_levels(memory_paths, "memory.limit_in_bytes", "memory.usage_in_bytes")
    return cpu, memory


def _member_path(root: Path, process_cgroup: Path, controller: str | None) -> Path:
    """用当前进程 cgroup 相对路径定位 controller；无记录或测试目录时回退根目录。"""
    text = _read_text(process_cgroup)
    if text is None:
        return root
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        controllers, relative = parts[1], parts[2]
        matches_v2 = controller is None and controllers == ""
        matches_v1 = controller is not None and bool(set(controller.split(",")) & set(controllers.split(",")))
        if matches_v2 or matches_v1:
            candidate = root / relative.lstrip("/")
            return candidate if candidate.exists() else root
    return root


def _visible_paths(root: Path, leaf: Path) -> list[Path]:
    """返回挂载根至进程叶组的可见祖先链；叶组不在挂载点下时只信任根组。"""
    try:
        relative = leaf.relative_to(root)
    except ValueError:
        return [root]
    paths = [root]
    current = root
    for part in relative.parts:
        current = current / part
        paths.append(current)
    return paths


def _memory_levels(paths: list[Path], limit_name: str, current_name: str) -> tuple[int, int] | None:
    """返回最小有限上限及最小余额；有限上限缺少用量时按零余额处理。"""
    levels: list[tuple[int, int]] = []
    for path in paths:
        limit = _nonnegative(_read_text(path / limit_name))
        if limit is None or limit >= 1 << 60:
            continue
        current = _nonnegative(_read_text(path / current_name))
        remaining = max(0, int(limit) - int(current)) if current is not None else 0
        levels.append((int(limit), remaining))
    if not levels:
        return None
    return min(limit for limit, _ in levels), min(remaining for _, remaining in levels)


def _available(host_available: int | None, memory: tuple[int, int] | None) -> int:
    """容器余额与宿主可用内存取较小值，避免 cgroup 余额掩盖节点整体内存压力。"""
    values = [value for value in (host_available, memory[1] if memory else None) if value is not None]
    return min(values) if values else 0


def _quota_limit(line: str | None) -> float | None:
    """解析 v2 有限 CPU 配额；max、缺失或损坏不构成额外限制。"""
    parts = (line or "").split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    return _quota_pair_limit(parts[0], parts[1])


def _quota_pair_limit(quota: str | None, period: str | None) -> float | None:
    """计算有限 CPU 核数配额，v1 的负 quota 代表不限制。"""
    amount, interval = _positive(quota), _positive(period)
    if amount is None or interval is None:
        return None
    return max(0.1, amount / interval)


def _nonnegative(value: object) -> float | None:
    """仅接受有限非负数；内存当前用量的零值是有效观测。"""
    if isinstance(value, bool):
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) and converted >= 0 else None
