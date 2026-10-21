"""小时归档下载的成员选择、重组和原包复用。

用户下载仅暴露日志成员。旧归档可能包含超过新格式限制的成员，导出时按
10 MiB 分卷；已完整选择的本地共享小时包则直接复用其原始压缩文件。
"""
from __future__ import annotations

import json
import os
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO
from zoneinfo import ZoneInfo

from camera_logs.logs.archive_access import (
    LimitedReader,
    LimitedWriter,
    copy_limited,
    gzip_tar_writer,
    snapshot,
)
from camera_logs.logs.naming import safe_filename_component, segment_filename, segment_name_from_source
from camera_logs.logs.order import ordered_files

MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_INDEX_LINE_BYTES = 16 * 1024
SHANGHAI = ZoneInfo("Asia/Shanghai")
Source = tuple[dict[str, Any], dict[str, Any], Path, bool]


@contextmanager
def _source_index(path: Path, file: dict[str, Any]) -> Iterator[BinaryIO | None]:
    """打开已随导出源固定的索引；没有可靠副本时明确返回空。"""
    pinned = file.get("_downloadIndexPath")
    if pinned:
        candidate = Path(str(pinned))
        if candidate.is_file():
            with candidate.open("rb") as source:
                yield source
                return
    if file.get("_downloadIndexPinned"):
        yield None
        return
    with tarfile.open(path, "r:gz") as archive:
        # 远端/临时源的 indexPath 只是源节点路径，不能在本节点回退打开；仅接受
        # 冻结 tar 内与原索引 basename 精确一致的成员，避免同包其它段误配。
        index_name = Path(str(file.get("indexPath") or "")).name
        try:
            member = archive.getmember(index_name) if index_name else None
        except KeyError:
            member = None
        if member is not None:
            source = archive.extractfile(member)
            if source is not None:
                try:
                    yield source
                finally:
                    source.close()
                return
    yield None


def _index_entries(source: BinaryIO) -> Iterator[tuple[int, int, datetime]]:
    """逐行校验索引位置和服务器接收时刻，拒绝无时区或模糊位置。"""
    position = 0
    while line := source.readline(MAX_INDEX_LINE_BYTES + 1):
        if len(line) > MAX_INDEX_LINE_BYTES:
            raise ValueError("日志索引单行超过上限")
        record = json.loads(line)
        if not isinstance(record, dict):
            raise TypeError("日志索引行不是对象")
        offset, length, received_at = (record.get(key) for key in ("offset", "length", "receivedAt"))
        if (not isinstance(offset, int) or isinstance(offset, bool) or not isinstance(length, int)
                or isinstance(length, bool) or offset != position or length <= 0):
            raise ValueError("日志索引不连续")
        stamp = datetime.fromisoformat(str(received_at))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("日志索引接收时间缺少时区")
        position += length
        yield offset, position, stamp


def _index_covers_waterline(path: Path, file: dict[str, Any], frozen_bytes: int) -> bool:
    """确认索引从正文首字节连续覆盖冻结水位，不把整点或首时间当作证据。"""
    if frozen_bytes <= 0:
        return False
    try:
        with _source_index(path, file) as source:
            if source is None:
                return False
            for _start, end, _stamp in _index_entries(source):
                if end >= frozen_bytes:
                    return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return False


class _ReceiptCursor:
    """按正文分卷顺序从已验证索引取相交接收时间，保持常量额外内存。"""

    def __init__(self, entries: Iterator[tuple[int, int, datetime]]) -> None:
        self.entries, self.current = entries, None

    def range(self, start: int, end: int) -> tuple[datetime | None, datetime | None]:
        """返回与半开字节范围相交的首末接收时间。"""
        while self.current is None or self.current[1] <= start:
            self.current = next(self.entries)
        first = last = None
        while self.current[0] < end:
            if self.current[1] > start:
                first = first or self.current[2]
                last = self.current[2]
            if self.current[1] >= end:
                break
            try:
                self.current = next(self.entries)
            except StopIteration:
                break
        return first, last


@contextmanager
def _receipt_cursor(path: Path, file: dict[str, Any], frozen_bytes: int) -> Iterator[_ReceiptCursor | None]:
    """只有完整验证通过后才暴露时间游标，避免后段坏索引制造前段真实时间名。"""
    if not _index_covers_waterline(path, file, frozen_bytes):
        yield None
        return
    with _source_index(path, file) as source:
        # 水位预验证已经读取过固定索引；这里发生的异常代表固定源损坏或实际
        # 导出失败，必须保留原始错误，不能在 contextmanager 中伪装为未知时间。
        yield _ReceiptCursor(_index_entries(source)) if source is not None else None


def pin_hour_sources(sources: list[Source], directory: Path, *, max_source_bytes: int | None = None) -> list[Source]:
    """校验前固定本地源归档 inode，校验和打包始终读取同一版本。"""
    pinned = {}
    result = []
    used = sum(path.stat().st_size for path in {path for _, _, path, temporary in sources if temporary})
    if max_source_bytes is not None and used > max_source_bytes:
        raise ValueError("export source storage limit exceeded")
    directory.mkdir(parents=True, exist_ok=True)
    for frozen, original_file, path, temporary in sources:
        file = original_file.copy()
        if not temporary:
            if path not in pinned:
                folder = directory / str(len(result))
                folder.mkdir()
                target = folder / path.name
                try:
                    os.link(path, target)
                except OSError:
                    # 跨挂载点只复制所选成员，不能为小水位复制整个大型小时包。
                    snapshot(path, target, int(frozen.get("bytes", file.get("bytes", 0))),
                             file_id=file.get("id"), archive_member=file.get("archiveMember"),
                             index_path=Path(str(file["indexPath"])) if file.get("indexPath") else None,
                             include_index=True,
                             max_output_bytes=None if max_source_bytes is None else max_source_bytes - used)
                    used += target.stat().st_size
                    result.append((frozen, file, target, True))
                    continue
                # 硬链接不复制正文，但仍按固定后的 inode 大小保守计入源集合预算。
                used += target.stat().st_size
                if max_source_bytes is not None and used > max_source_bytes:
                    target.unlink()
                    raise ValueError("export source storage limit exceeded")
                pinned[path] = target
            path = pinned[path]
            # READY 包本体和旁路索引都必须指向固定版本。索引不可用不阻断正文导出，
            # 由重组阶段生成 unknown-time 名，绝不改读可能被发布替换的原路径。
            file["_downloadIndexPinned"] = True
            source_index = Path(str(file.get("indexPath") or ""))
            if source_index.is_file():
                target_index = path.parent / source_index.name
                if not target_index.exists():
                    try:
                        os.link(source_index, target_index)
                    except OSError:
                        size = source_index.stat().st_size
                        if max_source_bytes is not None and used + size > max_source_bytes:
                            raise ValueError("export source storage limit exceeded")
                        copy_limited(source_index, target_index,
                                     max_output_bytes=None if max_source_bytes is None else max_source_bytes - used)
                    used += target_index.stat().st_size
                    if max_source_bytes is not None and used > max_source_bytes:
                        target_index.unlink(missing_ok=True)
                        raise ValueError("export source storage limit exceeded")
                file["_downloadIndexPath"] = str(target_index)
        result.append((frozen, file, path, temporary))
    return result


def hour_export_name(job: dict[str, Any], hour: object) -> str:
    """生成带上海自然小时起止时间的用户下载名，元数据缺失时保持安全稳定。"""
    task = safe_filename_component(str(job.get("taskName") or job.get("taskId") or "task"), max_utf8_bytes=80, fallback="task")
    ip = safe_filename_component(str(job.get("taskIp") or "unknown-ip"), max_utf8_bytes=64, fallback="unknown-ip")
    try:
        start = datetime.fromisoformat(str(hour)).astimezone(SHANGHAI)
        end = start + timedelta(hours=1)
        stamp = f"{start:%Y%m%d%H%M%S}-{end:%Y%m%d%H%M%S}"
    except (TypeError, ValueError):
        stamp = "unknown-hour"
    return f"{task}-{ip}-{stamp}.tar.gz"


@contextmanager
def source_member(path: Path, member_name: str | None) -> Iterator[tuple[tarfile.TarInfo, BinaryIO]]:
    """打开指定日志成员，并在调用方消费完成后回收归档和成员流。"""
    with tarfile.open(path, "r:gz") as archive:
        try:
            member = archive.getmember(member_name) if member_name else next((item for item in archive if item.name.endswith(".log")), None)
        except KeyError:
            member = None
        if member is None or not member.isfile() or not member.name.endswith(".log"):
            raise FileNotFoundError(member_name or path.name)
        stream = archive.extractfile(member)
        if stream is None:
            raise FileNotFoundError(member.name)
        try:
            yield member, stream
        finally:
            stream.close()


def write_hour_archive(destination: Path, sources: list[Source], *, task_name: object | None = None,
                       device_ip: object | None = None, max_output_bytes: int | None = None) -> int:
    """按稳定顺序重组一个小时，并将旧的大成员拆为最多 10 MiB 的分卷。"""
    by_id = {source[0]["id"]: source for source in sources}
    ordered = ordered_files([frozen | file for frozen, file, _, _ in sources])

    destination.parent.mkdir(parents=True, exist_ok=True)
    number = 1
    with destination.open("wb") as target, gzip_tar_writer(
        LimitedWriter(target, max_output_bytes, "export output exceeds size limit")
    ) as output:
        for document in ordered:
            frozen, file, path, _temporary = by_id[document["id"]]
            with source_member(path, file.get("archiveMember")) as (member, stream):
                frozen_bytes = int(frozen.get("bytes", member.size))
                if frozen_bytes < 0 or member.size < frozen_bytes:
                    raise OSError("日志字节数小于冻结水位")
                identity_task = task_name if task_name is not None else file.get("taskName") or file.get("taskId") or "task"
                identity_ip = device_ip if device_ip is not None else file.get("ip") or file.get("deviceIp") or "unknown-ip"
                remaining, offset = frozen_bytes, 0
                with _receipt_cursor(path, file, frozen_bytes) as receipts:
                    while remaining:
                        size = min(remaining, MAX_MEMBER_BYTES)
                        first, last = receipts.range(offset, offset + size) if receipts is not None else (None, None)
                        info = tarfile.TarInfo(segment_filename(identity_task, identity_ip, first, last, number))
                        info.size = size
                        output.addfile(info, LimitedReader(stream, size))
                        remaining -= size
                        offset += size
                        number += 1
                    if frozen_bytes == 0:
                        info = tarfile.TarInfo(segment_filename(identity_task, identity_ip, None, None, number))
                        info.size = 0
                        output.addfile(info)
                        number += 1
    return destination.stat().st_size


def reusable_hour_archive(sources: list[Source]) -> Path | None:
    """以已发布旁路清单确认完整选择的固定 READY 包，避免为 ZIP STORE 再解压全部正文。"""
    if not sources or any(temporary for _frozen, _file, _path, temporary in sources):
        return None
    paths = {path for _frozen, _file, path, _temporary in sources}
    groups = {str(file.get("archiveGroupId") or "") for _frozen, file, _path, _temporary in sources}
    if len(paths) != 1 or len(groups) != 1 or not next(iter(groups)):
        return None
    path = next(iter(paths))
    if any(frozen.get("status") != "READY" or not file.get("archiveMember") for frozen, file, _path, _temporary in sources):
        return None
    try:
        canonical_values = {str(file.get("path") or "") for _frozen, file, _path, _temporary in sources}
        if len(canonical_values) != 1 or not next(iter(canonical_values)):
            return None
        canonical_path = Path(next(iter(canonical_values)))
        canonical_before = canonical_path.stat()
        pinned_before = path.stat()
        identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        # job 固定的 hard link 必须仍指向发布目录的同一版本；原子替换后宁可重组，
        # 绝不能把旧 tar 与新 metadata 混用。旧成员 archiveBytes 可在后续合并后陈旧，
        # 不能用它否定完整 metadata 和冻结摘要。
        if identity(canonical_before) != identity(pinned_before):
            return None
        metadata_path = canonical_path.with_name(canonical_path.name + ".metadata.json")
        metadata_before = metadata_path.stat()
        # 发布顺序固定为先原子替换 tar、再原子替换旁路清单。因此 tar 比清单更新
        # 说明正处于两者之间的窗口，不能用上一版成员清单确认新版 tar。
        if metadata_before.st_size > MAX_METADATA_BYTES or metadata_before.st_mtime_ns < canonical_before.st_mtime_ns:
            return None
        with metadata_path.open(encoding="utf-8") as source:
            metadata = json.load(source)
        # 原子发布的旁路清单若在读取时被替换，宁可回退重建，也不把两个版本混用。
        metadata_after = metadata_path.stat()
        if (metadata_before.st_ino, metadata_before.st_size, metadata_before.st_mtime_ns) != (
            metadata_after.st_ino, metadata_after.st_size, metadata_after.st_mtime_ns,
        ):
            return None
        if identity(canonical_path.stat()) != identity(canonical_before):
            return None
        members = metadata.get("members") if isinstance(metadata, dict) and metadata.get("formatVersion") == 2 else None
        if not isinstance(members, list) or not members:
            return None
        by_name = {member.get("logName"): member for member in members if isinstance(member, dict)}
        selected = {str(file.get("archiveMember") or "") for _frozen, file, _path, _temporary in sources}
        if len(by_name) != len(members) or len(selected) != len(sources) or selected != set(by_name):
            return None
        for frozen, file, _path, _temporary in sources:
            member = by_name.get(file.get("archiveMember"))
            expected_bytes = int(frozen.get("bytes", -1))
            expected_hash = file.get("sha256") or frozen.get("sha256")
            source_name = member.get("sourceLogName") if isinstance(member, dict) else None
            first = _metadata_received_at(member.get("firstReceivedAt")) if isinstance(member, dict) else None
            last = _metadata_received_at(member.get("lastReceivedAt")) if isinstance(member, dict) else None
            if (not isinstance(member, dict) or not isinstance(source_name, str)
                    or member.get("logName") != segment_name_from_source(source_name, first, last)
                    or not str(member.get("logName", "")).endswith(".log")
                    or not isinstance(member.get("rawSize"), int) or member["rawSize"] != expected_bytes
                    or member["rawSize"] > MAX_MEMBER_BYTES or not expected_hash
                    or member.get("sha256") != expected_hash):
                return None
        return path
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _metadata_received_at(value: object) -> datetime | None:
    """解析归档清单已验证的服务器接收时间；无效值使快速复用回退到重组。"""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None and stamp.utcoffset() is not None else None
