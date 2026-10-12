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

from camera_logs.logs.archive_access import LimitedReader, LimitedWriter, gzip_tar_writer, snapshot
from camera_logs.logs.naming import safe_filename_component
from camera_logs.logs.order import ordered_files

MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
SHANGHAI = ZoneInfo("Asia/Shanghai")
Source = tuple[dict[str, Any], dict[str, Any], Path, bool]


def pin_hour_sources(sources: list[Source], directory: Path, *, max_source_bytes: int | None = None) -> list[Source]:
    """校验前固定本地源归档 inode，校验和打包始终读取同一版本。"""
    pinned = {}
    result = []
    used = sum(path.stat().st_size for path in {path for _, _, path, temporary in sources if temporary})
    if max_source_bytes is not None and used > max_source_bytes:
        raise ValueError("export source storage limit exceeded")
    directory.mkdir(parents=True, exist_ok=True)
    for frozen, file, path, temporary in sources:
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


def write_hour_archive(destination: Path, sources: list[Source], *, max_output_bytes: int | None = None) -> int:
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
                remaining = frozen_bytes
                while remaining:
                    size = min(remaining, MAX_MEMBER_BYTES)
                    info = tarfile.TarInfo(f"part-{number:06d}.log")
                    info.size = size
                    output.addfile(info, LimitedReader(stream, size))
                    remaining -= size
                    number += 1
                if frozen_bytes == 0:
                    info = tarfile.TarInfo(f"part-{number:06d}.log")
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
            if (not isinstance(member, dict) or not str(member.get("logName", "")).endswith(".log")
                    or not isinstance(member.get("rawSize"), int) or member["rawSize"] != expected_bytes
                    or member["rawSize"] > MAX_MEMBER_BYTES or not expected_hash
                    or member.get("sha256") != expected_hash):
                return None
        return path
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
