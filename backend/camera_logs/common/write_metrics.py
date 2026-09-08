"""有界记录接收批次到文件可读的延迟，使用单调时钟避免受服务器时间回拨影响。"""

import math
import time
from collections.abc import Callable

WINDOW_SECONDS = 60


class WriteLatency:
    """按单调秒聚合最近窗口的保守延迟直方图及一个未完成写入。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._buckets: dict[int, dict[int, int]] = {}
        self._histogram: dict[int, int] = {}
        self._samples = 0
        self._pending: float | None = None

    def begin(self) -> None:
        """收到该批次首块时开始计时，后续块不能重置已经等待的时间。"""
        if self._pending is None:
            self._pending = self._clock()

    def finish(self) -> None:
        """只有批次写入成功才计入样本，直方图值始终取延迟的保守上界。"""
        if self._pending is None:
            return
        instant = self._clock()
        self._expire(instant)
        delay = max(0, instant-self._pending) * 1000
        upper_bound = self._upper_bound(delay)
        bucket = self._buckets.setdefault(math.floor(instant), {})
        bucket[upper_bound] = bucket.get(upper_bound, 0) + 1
        self._histogram[upper_bound] = self._histogram.get(upper_bound, 0) + 1
        self._samples += 1
        self._pending = None

    def snapshot(self) -> dict[str, float | int]:
        """返回最近至多 61 个秒桶的保守 P99，未完成写入另报等待年龄。"""
        instant = self._clock()
        self._expire(instant)
        rank = math.ceil(self._samples * .99)
        total = 0
        p99 = 0
        for upper_bound in sorted(self._histogram):
            total += self._histogram[upper_bound]
            if total >= rank:
                p99 = upper_bound
                break
        return {
            "p99Ms": p99,
            "samples": self._samples,
            "pendingMs": max(0, instant-self._pending) * 1000 if self._pending is not None else 0,
            "windowSeconds": WINDOW_SECONDS,
        }

    def _expire(self, instant: float) -> None:
        """桶内全部样本早于窗口下界时移除，秒桶上限保持为 61 个。"""
        cutoff = instant-WINDOW_SECONDS
        for second, bucket in list(self._buckets.items()):
            if second+1 > cutoff:
                continue
            del self._buckets[second]
            for upper_bound, count in bucket.items():
                remaining = self._histogram[upper_bound]-count
                if remaining:
                    self._histogram[upper_bound] = remaining
                else:
                    del self._histogram[upper_bound]
                self._samples -= count

    @staticmethod
    def _upper_bound(delay_ms: float) -> int:
        """一秒内以毫秒上界统计，超过一秒使用下一档 2 的幂保持有限 bin 种类。"""
        rounded = math.ceil(delay_ms)
        if rounded <= 1000:
            return rounded
        return 1 << (rounded-1).bit_length()
