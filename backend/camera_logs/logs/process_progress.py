"""压缩子进程向父进程报告真实文件处理进度，供无进展 watchdog 使用。"""

from __future__ import annotations

import time

_PHASES = {"IDLE": 0, "HASH": 1, "TAR": 2, "VERIFY": 3, "INDEX": 4}
_NAMES = {value: key for key, value in _PHASES.items()}
_progress = None


def create(context):
    """创建可由 spawn 子进程继承的最后进度时间、字节数和阶段。"""
    # 单个压缩子进程是唯一写者；无锁标量避免子进程在持锁崩溃时反向卡住 watchdog。
    return (context.Value("d", time.monotonic(), lock=False), context.Value("Q", 0, lock=False),
            context.Value("i", _PHASES["IDLE"], lock=False))


def install(progress) -> None:
    """由进程池 initializer 安装共享状态，工作函数无需携带额外参数。"""
    global _progress
    _progress = progress


def advance(phase: str, size: int = 0) -> None:
    """仅在真实处理数据块后推进，节流等待和挂起不会伪造心跳。"""
    if _progress is None:
        return
    stamp, bytes_processed, current_phase = _progress
    bytes_processed.value += max(0, size)
    current_phase.value = _PHASES[phase]
    stamp.value = time.monotonic()


def snapshot(progress) -> tuple[float, int, str]:
    """读取最近一次真实进度，供父进程在超时日志中说明停滞阶段。"""
    stamp, bytes_processed, current_phase = progress
    return stamp.value, bytes_processed.value, _NAMES.get(current_phase.value, "UNKNOWN")
