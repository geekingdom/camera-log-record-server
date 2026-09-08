"""小时归档下载的成员选择、重组和原包复用。

用户下载仅暴露日志成员。旧归档可能包含超过新格式限制的成员，导出时按
10 MiB 分卷；已完整选择的本地共享小时包则直接复用其原始压缩文件。
"""
from __future__ import annotations

import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO
from zoneinfo import ZoneInfo

from camera_logs.logs.archive_access import LimitedReader
from camera_logs.logs.naming import safe_filename_component

MAX_MEMBER_BYTES = 10 * 1024 * 1024
SHANGHAI = ZoneInfo("Asia/Shanghai")
Source = tuple[dict[str, Any], dict[str, Any], Path, bool]


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


def write_hour_archive(destination: Path, sources: list[Source]) -> int:
    """按稳定顺序重组一个小时，并将旧的大成员拆为最多 10 MiB 的分卷。"""
    def order(item: Source) -> tuple[str, str, int, int, str]:
        frozen, file, _path, _temporary = item
        return (
            str(file.get("runStartedAt") or frozen.get("runStartedAt") or ""),
            str(file.get("sessionStartedAt") or frozen.get("sessionStartedAt") or ""),
            int(file.get("firstSequence") or frozen.get("firstSequence") or 0),
            int(file.get("segmentNumber") or frozen.get("segmentNumber") or 0),
            str(frozen.get("id")),
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    number = 1
    with tarfile.open(destination, "w:gz", compresslevel=1) as output:
        for frozen, file, path, _temporary in sorted(sources, key=order):
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
    """确认完整选中同一共享包的全部日志成员后，返回可直接下载的原始包。"""
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
        with tarfile.open(path, "r:gz") as archive:
            all_members = archive.getmembers()
            members = [item for item in all_members if item.isfile()]
            if not members or len(members) != len(all_members) or any(not item.name.endswith(".log") or item.size > MAX_MEMBER_BYTES for item in members):
                return None
            selected = {str(file["archiveMember"]) for _frozen, file, _path, _temporary in sources}
            names = [item.name for item in members]
            if len(set(names)) != len(names) or len(selected) != len(sources) or selected != set(names):
                return None
            sizes = {item.name: item.size for item in members}
            return path if all(int(frozen.get("bytes", -1)) == sizes[file["archiveMember"]] for frozen, file, _path, _temporary in sources) else None
    except (OSError, tarfile.TarError):
        return None
