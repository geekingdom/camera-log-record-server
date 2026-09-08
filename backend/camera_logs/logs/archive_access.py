"""固定水位快照和节点共用的读取限速。

读取上限对搜索、复制和快照统一计费；快照只复制已经登记的字节，不能因
文件继续追加而增长，也不能在文件缺失时静默缩短结果。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO

from camera_logs.logs.source import open_log_source

READ_RATE = 50 * 1024 * 1024


class ReadLimiter:
    """通过单调时钟和线程锁预约带宽，所有阻塞等待必须在工作线程调用。"""
    def __init__(self, rate: int = READ_RATE) -> None:
        self.rate, self.next_at, self.lock = rate, 0.0, threading.Lock()

    def consume(self, size: int) -> None:
        with self.lock:
            now = time.monotonic()
            target = max(now, self.next_at)
            self.next_at = target + size / self.rate
        if target > now:
            time.sleep(target - now)


read_limiter = ReadLimiter()


class LimitedWriter:
    """在产物字节落盘前检查上限；流式接口让 gzip/ZIP 尾部也纳入计费。"""

    def __init__(self, output: BinaryIO, limit: int | None, error: str = "snapshot storage limit exceeded") -> None:
        self.output, self.limit, self.written = output, limit, 0
        self.error = error

    def write(self, data: bytes) -> int:
        if self.limit is not None and self.written + len(data) > self.limit:
            raise ValueError(self.error)
        size = self.output.write(data)
        self.written += size
        return size

    def tell(self) -> int:
        return self.written

    def flush(self) -> None:
        self.output.flush()


class LimitedReader:
    """把流限制在冻结字节数内，同时计费读带宽并可选累计 SHA-256。"""
    def __init__(self, source: BinaryIO, size: int, digest=None) -> None:
        self.source, self.remaining, self.digest = source, size, digest

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        data = self.source.read(self.remaining if size < 0 else min(size, self.remaining))
        self.remaining -= len(data)
        read_limiter.consume(len(data))
        if self.digest:
            self.digest.update(data)
        return data


def snapshot(path: Path, target: Path, raw_bytes: int, index_path: Path | None = None, file_id: str | None = None, archive_member: str | None = None, include_index: bool = False, *, max_output_bytes: int | None = None) -> Path:
    """复制冻结正文前缀和索引，附带摘要清单；不允许把缺失尾部当成完整快照。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        source = stack.enter_context(open_log_source(path, archive_member))
        index, index_name, index_size = None, None, 0
        if include_index and index_path and index_path.is_file():
            index = stack.enter_context(index_path.open("rb"))
            index_name, index_size = index_path.name, os.fstat(index.fileno()).st_size
        elif include_index and source.archive:
            embedded = next((item for item in source.archive if item.isfile() and item.name.endswith(".index.jsonl")), None)
            if embedded:
                index = source.archive.extractfile(embedded)
                if index:
                    stack.callback(index.close)
                    index_name, index_size = embedded.name, embedded.size
        size, raw_hash, index_hash = max(0, raw_bytes), hashlib.sha256(), hashlib.sha256()
        if source.size < raw_bytes:
            raise OSError("日志字节数小于冻结水位，无法生成完整快照")
        with target.open("wb") as destination, tarfile.open(fileobj=LimitedWriter(destination, max_output_bytes), mode="w:gz", compresslevel=1) as output:
            raw_info = tarfile.TarInfo(source.name); raw_info.size = size
            output.addfile(raw_info, LimitedReader(source.stream, size, raw_hash))
            if index and index_name:
                index_info = tarfile.TarInfo(index_name); index_info.size = index_size
                output.addfile(index_info, LimitedReader(index, index_size, index_hash))
            manifest = {"fileId": file_id, "rawBytes": size, "sha256": raw_hash.hexdigest(), "indexBytes": index_size, "indexSha256": index_hash.hexdigest() if index else None}
            data = json.dumps(manifest, sort_keys=True).encode(); info = tarfile.TarInfo("manifest.json"); info.size = len(data)
            output.addfile(info, io.BytesIO(data))
    return target


def copy_limited(source: Path, target: Path, *, max_output_bytes: int | None = None) -> int:
    """分块复制归档并计费读取预算，避免大型导出驻留内存。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("wb") as destination:
        writer = LimitedWriter(destination, max_output_bytes, "export output exceeds size limit")
        while data := reader.read(1024 * 1024):
            read_limiter.consume(len(data)); writer.write(data)
    return target.stat().st_size
