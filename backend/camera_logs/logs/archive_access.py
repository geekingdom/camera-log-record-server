"""固定水位快照和节点共用的读取限速。

读取上限对搜索、复制和快照统一计费；快照只复制已经登记的字节，不能因
文件继续追加而增长，也不能在文件缺失时静默缩短结果。
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
import time
from pathlib import Path
from typing import BinaryIO

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


class SnapshotWriter:
    """在压缩字节落盘前检查预留上限，覆盖 gzip 文件头和关闭时的尾部。"""

    def __init__(self, output: BinaryIO, limit: int | None) -> None:
        self.output, self.limit, self.written = output, limit, 0

    def write(self, data: bytes) -> int:
        if self.limit is not None and self.written + len(data) > self.limit:
            raise ValueError("snapshot storage limit exceeded")
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


def _members(path: Path, index_path: Path | None, archive_member: str | None = None, include_index: bool = False):
    """统一打开未压缩文件和归档成员，返回的资源由快照调用者负责关闭。"""
    if path.suffixes[-2:] == [".tar", ".gz"]:
        archive = tarfile.open(path, "r:gz")  # noqa: SIM115 - ownership spans returned member streams
        try:
            raw = archive.getmember(archive_member) if archive_member else next((item for item in archive.getmembers() if item.name.endswith(".log")), None)
        except KeyError:
            raw = None
        if raw and (not raw.isfile() or not raw.name.endswith(".log")):
            raw = None
        if raw is None:
            archive.close()
            raise FileNotFoundError("archive contains no raw log")
        embedded = next((item for item in archive.getmembers() if item.name.endswith(".index.jsonl")), None)
        if include_index and index_path and index_path.is_file():
            index = index_path.open("rb")
            return archive, archive.extractfile(raw), raw.name, raw.size, index, index_path.name, index_path.stat().st_size
        return archive, archive.extractfile(raw), raw.name, raw.size, archive.extractfile(embedded) if include_index and embedded else None, embedded.name if include_index and embedded else None, embedded.size if include_index and embedded else 0
    index = index_path.open("rb") if include_index and index_path and index_path.is_file() else None
    return None, path.open("rb"), path.name, path.stat().st_size, index, index_path.name if index else None, index_path.stat().st_size if index else 0


def snapshot(path: Path, target: Path, raw_bytes: int, index_path: Path | None = None, file_id: str | None = None, archive_member: str | None = None, include_index: bool = False, *, max_output_bytes: int | None = None) -> Path:
    """复制冻结正文前缀和索引，附带摘要清单；不允许把缺失尾部当成完整快照。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    archive, raw, raw_name, available, index, index_name, index_size = _members(path, index_path, archive_member, include_index)
    if raw is None:
        raise FileNotFoundError(path)
    size, raw_hash, index_hash = min(max(0, raw_bytes), available), hashlib.sha256(), hashlib.sha256()
    try:
        if available < raw_bytes:
            raise OSError("日志字节数小于冻结水位，无法生成完整快照")
        with target.open("wb") as destination, tarfile.open(fileobj=SnapshotWriter(destination, max_output_bytes), mode="w:gz", compresslevel=1) as output:
            raw_info = tarfile.TarInfo(raw_name); raw_info.size = size
            output.addfile(raw_info, LimitedReader(raw, size, raw_hash))
            if index and index_name:
                index_info = tarfile.TarInfo(index_name); index_info.size = index_size
                output.addfile(index_info, LimitedReader(index, index_size, index_hash))
            manifest = {"fileId": file_id, "rawBytes": size, "sha256": raw_hash.hexdigest(), "indexBytes": index_size, "indexSha256": index_hash.hexdigest() if index else None}
            data = json.dumps(manifest, sort_keys=True).encode(); info = tarfile.TarInfo("manifest.json"); info.size = len(data)
            output.addfile(info, io.BytesIO(data))
    finally:
        raw.close()
        if index: index.close()
        if archive: archive.close()
    return target


def copy_limited(source: Path, target: Path) -> int:
    """分块复制归档并计费读取预算，避免大型导出驻留内存。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("wb") as writer:
        while data := reader.read(1024 * 1024):
            read_limiter.consume(len(data)); writer.write(data)
    return target.stat().st_size
