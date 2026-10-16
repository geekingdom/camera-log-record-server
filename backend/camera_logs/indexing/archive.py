"""旁路索引的已发布归档顺序读取；单个 tar.gz 每轮只解压一次。"""

from __future__ import annotations

import gzip
import tarfile
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx

from camera_logs.indexing.ingest import source_path
from camera_logs.logs.archive_access import LimitedReader


def archive_key(settings: Any, file: dict) -> str | None:
    """返回可顺序读取的小时包身份；原始文件继续走逐文件读取路径。"""
    try:
        path = source_path(settings, file)
    except (KeyError, TypeError, ValueError):
        return None
    if path.suffixes[-2:] != [".tar", ".gz"] or not file.get("archiveMember"):
        return None
    return str(path)


def visit_archive_members(
    settings: Any,
    files: list[dict],
    visitor: Callable[[dict, Any, int], Any],
) -> dict[str, Any]:
    """按包内物理顺序访问成员，跳过字节与命中成员均纳入读取预算。

    每个成员的异常作为结果返回，后续成员仍会继续；缺失成员同样只影响自身。
    """
    if not files:
        return {}
    value = archive_key(settings, files[0])
    if value is None:
        raise ValueError("归档来源无效")
    path = Path(value)
    pending = {str(file["archiveMember"]): file for file in files}
    if len(pending) != len(files):
        raise ValueError("同一归档存在重复成员目录")
    results: dict[str, Any] = {}
    with ExitStack() as resources:
        source = resources.enter_context(path.open("rb"))
        # 限制 gzip 解压后的逻辑 tar 字节，不能仅按很小的压缩输入计费。
        decompressed = resources.enter_context(gzip.GzipFile(fileobj=source, mode="rb"))
        archive = resources.enter_context(tarfile.open(
            fileobj=LimitedReader(decompressed, 2**63 - 1), mode="r|"
        ))
        for member in archive:
            file = pending.pop(member.name, None)
            if file is None:
                continue
            if not member.isfile() or not member.name.endswith(".log") or member.size != int(file["bytes"]):
                results[file["id"]] = FileNotFoundError(member.name)
                continue
            stream = archive.extractfile(member)
            if stream is None:
                results[file["id"]] = FileNotFoundError(member.name)
                continue
            with stream:
                try:
                    results[file["id"]] = visitor(file, stream, member.size)
                except Exception as error:
                    if isinstance(error, (httpx.TransportError, httpx.HTTPStatusError)):
                        raise
                    results[file["id"]] = error
    for file in pending.values():
        results[file["id"]] = FileNotFoundError(str(file["archiveMember"]))
    return results
