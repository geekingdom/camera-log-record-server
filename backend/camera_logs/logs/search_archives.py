"""共享小时归档的顺序扫描，避免为每个已发布成员重复解压同一 tar.gz。"""

from __future__ import annotations

import tarfile
from contextlib import ExitStack, contextmanager
from os import fstat
from pathlib import Path
from typing import Any

from camera_logs.logs.archive_access import LimitedReader
from camera_logs.logs.search_stream import index_entries


class _CancelledReader(LimitedReader):
    """在 tar 顺序解压的每次底层读取前检查停止信号，避免跳过大成员时延迟取消。"""

    def __init__(self, source, size, cancelled):
        super().__init__(source, size)
        self.cancelled = cancelled

    def read(self, size=-1):
        if self.cancelled():
            raise InterruptedError("job cancelled")
        return super().read(size)


@contextmanager
def _stream_archive(path: Path, cancelled):
    """把压缩输入也接入读取预算，并由调用者在同一线程统一关闭全部句柄。"""
    with path.open("rb") as source, tarfile.open(
        fileobj=_CancelledReader(source, fstat(source.fileno()).st_size, cancelled), mode="r|gz"
    ) as archive:
        yield archive


class ReadyArchiveGroup:
    """在固定专用线程中复用一个已发布小时包，同时保留成员级调度检查点。"""

    def __init__(self, path: Path, scanner: Any, cancelled) -> None:
        self.path, self.scanner, self.cancelled = path, scanner, cancelled
        self.archive = None
        self.resources = None

    def open(self):
        """建立一次 gzip/tar 目录；同组成员只从此不可变句柄提取正文。"""
        self.resources = ExitStack()
        self.archive = self.resources.enter_context(_stream_archive(self.path, self.cancelled))

    def scan_member(self, file: dict, index_path: Path, limit: int, text_limit: int, cancelled):
        """扫描一个成员及其独立旁路索引，保持现有结果预算和流式取消合同。"""
        if self.archive is None:
            raise RuntimeError("archive group is not open")
        if cancelled():
            raise InterruptedError("job cancelled")
        member_name = file.get("archiveMember")
        raw = self._locate(member_name, cancelled)
        if not raw.isfile() or not raw.name.endswith(".log") or raw.size != int(file["bytes"]):
            raise FileNotFoundError(member_name or self.path.name)
        stream = self.archive.extractfile(raw)
        if stream is None:
            raise FileNotFoundError(raw.name)
        results, text_bytes = [], 0
        with stream, index_path.open("rb") as index_stream:
            entries = index_entries(LimitedReader(index_stream, index_path.stat().st_size), cancelled)
            for result in self.scanner.scan(LimitedReader(stream, raw.size), entries, file, cancelled):
                size = len(result.get("text", "").encode("utf-8"))
                if text_bytes + size > text_limit:
                    return results, text_bytes, True
                results.append(result)
                text_bytes += size
                if len(results) >= limit:
                    return results, text_bytes, True
        return results, text_bytes, False

    def _locate(self, member_name, cancelled):
        """顺序游标未命中时只重开一次，兼容旧包成员物理顺序与冻结顺序不一致。"""
        for restart in range(2):
            while raw := self.archive.next():
                if cancelled():
                    raise InterruptedError("job cancelled")
                if raw.name == member_name:
                    return raw
            if restart == 0:
                self.close()
                self.open()
        raise FileNotFoundError(member_name or self.path.name)

    def close(self):
        """在创建句柄的同一线程关闭归档，避免 gzip 缓冲跨线程释放。"""
        if self.resources is not None:
            self.resources.close()
            self.resources = None
            self.archive = None
