"""采集正文落盘专用的有界线程池，隔离解压和其他默认线程任务。"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
from typing import Any

MAX_WORKERS = 32
DEFAULT_CONCURRENCY = 4
_executor: ThreadPoolExecutor | None = None
_limit = DEFAULT_CONCURRENCY
_active = 0
_waiters = 0
_closed = False
_changed = asyncio.Event()
_queue_samples: deque[float] = deque(maxlen=1024)
_execution_samples: deque[float] = deque(maxlen=1024)
_stage_samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=1024))
_metrics_lock = threading.Lock()
logger = logging.getLogger(__name__)
_latency = ContextVar("physical_write_latency", default=None)


@contextmanager
def measure_io(tracker):
    """仅把实际文件操作交给调用会话计时，归档排队和文件锁等待不算磁盘I/O。"""
    token = _latency.set(tracker)
    try:
        yield
    finally:
        _latency.reset(token)


def configure(writeConcurrency: int = DEFAULT_CONCURRENCY) -> None:
    """更新新提交的并发门槛；缩容只等待在途操作排空，绝不取消已写入批次。"""
    global _limit, _closed
    if isinstance(writeConcurrency, bool) or not isinstance(writeConcurrency, int):
        raise TypeError("writeConcurrency must be an integer")
    if not 1 <= writeConcurrency <= MAX_WORKERS:
        raise ValueError(f"writeConcurrency must be between 1 and {MAX_WORKERS}")
    _limit, _closed = writeConcurrency, False
    _changed.set()
    _reset_gate_if_idle()


def _reset_gate_if_idle() -> None:
    """旧事件循环已无等待者时重建门控，避免测试和重启复用已绑定 Event。"""
    global _changed
    if _active == 0 and _waiters == 0:
        _changed = asyncio.Event()


async def _acquire() -> None:
    """在提交线程前取得逻辑槽位，避免 ThreadPoolExecutor 内部请求队列无界增长。"""
    global _active, _waiters
    while True:
        if _closed:
            raise RuntimeError("采集写盘执行器已关闭")
        if _active < _limit:
            _active += 1
            return
        observed = _changed
        _waiters += 1
        try:
            await observed.wait()
        finally:
            _waiters -= 1
        if observed is _changed:
            _changed.clear()


def _release() -> None:
    """仅在线程 future 已收尾后归还槽位，并唤醒因缩容或满载等待的提交者。"""
    global _active
    _active -= 1
    _changed.set()


def _execute(submitted: float, function, args, kwargs):
    """在线程中记录实际开始和结束时间，等待时间包含门槛及 executor 调度。"""
    started = time.monotonic()
    with _metrics_lock:
        _queue_samples.append((started - submitted) * 1000)
    try:
        return function(*args, **kwargs)
    finally:
        with _metrics_lock:
            _execution_samples.append((time.monotonic() - started) * 1000)


async def write_thread(function, *args, **kwargs):
    """执行一次采集文件操作；取消仍等待线程退出，不重复执行写入函数。"""
    global _executor
    submitted = time.monotonic()
    await _acquire()
    future = None
    tracker = _latency.get()
    if tracker is not None:
        tracker.begin()
    try:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="log-write")
        future = asyncio.get_running_loop().run_in_executor(
            _executor, partial(_execute, submitted, function, args, kwargs))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 - 取消时取回线程错误后仍传播原取消。
                    break
            if not future.cancelled():
                try:
                    future.result()
                except Exception:  # 后台错误只作收尾诊断，不能替换调用方取消。
                    logger.exception("采集写盘取消期间线程失败")
            raise
    finally:
        if tracker is not None:
            if future is not None and future.done() and not future.cancelled() and future.exception() is None:
                tracker.finish()
            else:
                tracker.abort()
        _release()


def record_stage(stage: str, elapsed_ms: float) -> None:
    """记录正文、索引和 fsync 的阶段耗时；样本有界且不影响落盘结果。"""
    if isinstance(stage, str) and stage and elapsed_ms >= 0:
        with _metrics_lock:
            _stage_samples[stage].append(float(elapsed_ms))


def _summary(samples: list[float]) -> dict[str, float | int]:
    values = sorted(samples)
    if not values:
        return {"samples": 0, "sampleWindow": 1024, "p99Ms": 0.0}
    return {"samples": len(values), "sampleWindow": 1024,
            "p99Ms": values[min(len(values) - 1, int(len(values) * .99))]}


def snapshot() -> dict[str, Any]:
    """返回有界调度与阶段指标，供节点心跳/诊断读取而不访问采集正文。"""
    with _metrics_lock:
        queue = list(_queue_samples)
        execution = list(_execution_samples)
        stages = {name: list(samples) for name, samples in _stage_samples.items()}
    return {
        "concurrency": _limit, "active": _active, "waiting": _waiters,
        "queue": _summary(queue), "execution": _summary(execution),
        "stages": {name: _summary(samples) for name, samples in stages.items()},
    }


async def shutdown() -> None:
    """停止新提交并等待全部已提交落盘线程退出，之后释放线程资源。"""
    global _executor, _closed
    _closed = True
    _changed.set()
    if _executor is not None:
        executor = _executor
        closing = asyncio.create_task(asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=False))
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
        closing.result()
        _executor = None
        if cancelled:
            _closed = False
            _reset_gate_if_idle()
            raise asyncio.CancelledError
    # 测试与应用重启可能使用新的事件循环；等待者排空后才可丢弃旧 Event。
    _closed = False
    _reset_gate_if_idle()
