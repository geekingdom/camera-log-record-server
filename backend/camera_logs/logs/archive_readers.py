"""有界复用归档顺序解压句柄；请求独占游标，原始日志仍按次打开。"""

import threading
import time
from collections import OrderedDict
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from camera_logs.logs.archive_access import LimitedReader, read_limiter
from camera_logs.logs.source import LogSource, file_version, open_log_source


@dataclass
class _Reader:
    """空闲缓存之外的条目归当前工作线程独占，禁止并发移动同一个流。"""
    stack: ExitStack
    source: LogSource
    touched: float


class ArchiveReaders:
    """最多保留 128 个空闲句柄，另有受上层四线程限制的在用句柄。

    缓存键包含采集文件身份、路径版本、精确成员和下一字节偏移。借用时从缓存
    移除，返回时重新登记；重复或交错查询不能共享一个正在移动的解压游标。
    """

    def __init__(self, capacity=128, idle_seconds=30):
        if capacity < 1 or idle_seconds <= 0:
            raise ValueError("归档读取缓存容量与空闲时间必须为正数")
        self.capacity, self.idle_seconds = capacity, idle_seconds
        self._idle = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False

    def prune(self):
        """清理空闲超时条目；由节点周期任务调用，完全无请求时也主动回收句柄。"""
        threshold = time.monotonic() - self.idle_seconds
        with self._lock:
            expired = [key for key, entry in self._idle.items() if entry.touched <= threshold]
            for key in expired:
                self._idle.pop(key).stack.close()

    def close(self):
        """禁止新借用并释放空闲资源；在用条目归还时发现关闭会立即释放。"""
        with self._lock:
            self._closed = True
            while self._idle:
                self._idle.popitem()[1].stack.close()

    def _take(self, key):
        self.prune()
        with self._lock:
            if self._closed:
                raise RuntimeError("归档读取缓存已关闭")
            # 相同路径的旧 inode 即使还有可读数据，也不能供后续请求命中。
            stale = [old for old in self._idle if old[1] == key[1] and old[2] != key[2]]
            for old in stale:
                self._idle.pop(old).stack.close()
            return self._idle.pop(key, None)

    def _put(self, key, entry):
        with self._lock:
            if self._closed:
                entry.stack.close()
                return
            previous = self._idle.pop(key, None)
            if previous is not None:
                previous.stack.close()
            entry.touched = time.monotonic()
            self._idle[key] = entry
            while len(self._idle) > self.capacity:
                self._idle.popitem(last=False)[1].stack.close()

    def read(self, identity, path: Path, member, offset, limit, watermark):
        """按请求冻结水位读取；首次随机定位计费前缀，顺序命中仅计费新增正文。"""
        archived = path.suffixes[-2:] == [".tar", ".gz"]
        version = file_version(path.stat()) if archived else None
        key = (identity, path, version, member, offset)
        entry = self._take(key)
        if entry is None:
            stack = ExitStack()
            try:
                source = stack.enter_context(open_log_source(path, member))
            except BaseException:
                stack.close()
                raise
            entry = _Reader(stack, source, time.monotonic())
        try:
            source = entry.source
            cap = min(int(watermark) if watermark is not None else source.size, source.size)
            if offset >= cap:
                return b""
            if source.archive is None:
                source.stream.seek(offset)
                data = source.stream.read(min(limit, cap - offset))
                read_limiter.consume(len(data))
                return data
            position = source.stream.tell()
            stream = LimitedReader(source.stream, cap - position)
            remaining = offset - position
            while remaining > 0:
                skipped = stream.read(min(262144, remaining))
                if not skipped:
                    return b""
                remaining -= len(skipped)
            data = stream.read(limit)
            next_offset = offset + len(data)
            # 原路径回退的临时归档句柄不缓存；只复用已确认版本且仍有正文的归档。
            if archived and source.version == version and data and next_offset < source.size:
                self._put(key[:-1] + (next_offset,), entry)
                entry = None
            return data
        finally:
            if entry is not None:
                entry.stack.close()
