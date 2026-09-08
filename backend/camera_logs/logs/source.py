"""固定原始日志或归档成员的打开句柄，跨越归档删除与目录发布之间的窗口。"""

from __future__ import annotations

import json
import os
import tarfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class LogSource:
    """一次读取使用的不可变源身份；大小来自已打开句柄，不再次按路径查询。"""

    stream: BinaryIO
    name: str
    size: int
    archive: tarfile.TarFile | None = None
    version: tuple | None = None


def file_version(stat: os.stat_result) -> tuple:
    """文件句柄与路径用相同指纹识别原子替换及原地修改，不只按名称复用。"""
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _published_member(path: Path) -> tuple[Path, int]:
    """仅由同目录正式清单定位同名正文，拒绝临时包、歧义和跨目录符号链接。"""
    matches = []
    parent = path.parent.resolve()
    for metadata_path in parent.glob("*.tar.gz.metadata.json"):
        if metadata_path.resolve().parent != parent:
            raise ValueError("archive metadata is outside log directory")
        with metadata_path.open(encoding="utf-8") as handle:
            metadata = json.load(handle)
        if metadata.get("formatVersion") != 2:
            continue
        for member in metadata.get("members", []):
            if member.get("logName") == path.name:
                archive = metadata_path.with_name(metadata_path.name.removesuffix(".metadata.json"))
                if archive.resolve().parent != parent:
                    raise ValueError("archive is outside log directory")
                matches.append((archive, int(member["rawSize"])))
    if len(matches) != 1:
        raise FileNotFoundError(f"published archive member is missing or ambiguous: {path.name}")
    return matches[0]


@contextmanager
def open_log_source(path: Path, member_name: str | None = None):
    """原文件打开失败时读已发布的同名分卷；所有句柄随本次读取退出而关闭。

    压缩发布在删除原文件之前完成。先打开原文件可保留其 inode；若删除抢先完成，
    正式清单及小时包已经存在。此流程无需等待 MongoDB 目录刷新，也不重试已读取字节。
    """
    with ExitStack() as stack:
        expected_size = None
        if path.suffixes[-2:] != [".tar", ".gz"]:
            try:
                raw = stack.enter_context(path.open("rb"))
            except FileNotFoundError:
                if path.suffix != ".log":
                    raise
                member_name = path.name
                path, expected_size = _published_member(path)
            else:
                yield LogSource(raw, path.name, os.fstat(raw.fileno()).st_size)
                return
        archive_file = stack.enter_context(path.open("rb"))
        version = file_version(os.fstat(archive_file.fileno()))
        archive = stack.enter_context(tarfile.open(fileobj=archive_file, mode="r:gz"))
        try:
            member = archive.getmember(member_name) if member_name else next(
                (item for item in archive if item.isfile() and item.name.endswith(".log")), None)
        except KeyError:
            member = None
        if member is None or not member.isfile() or not member.name.endswith(".log"):
            raise FileNotFoundError(member_name or path.name)
        if expected_size is not None and member.size != expected_size:
            raise OSError("published archive member size differs from metadata")
        raw = archive.extractfile(member)
        if raw is None:
            raise FileNotFoundError(member.name)
        stack.callback(raw.close)
        yield LogSource(raw, member.name, member.size, archive, version)
