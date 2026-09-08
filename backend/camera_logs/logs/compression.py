"""独立进程合并小时日志包，并以旁路元数据保存索引与成员清单。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import tarfile
import tempfile
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_pool: ProcessPoolExecutor | None = None


def _hash_stream(stream):
    """分块计算摘要和字节数，避免大文件在压缩进程中占用整块内存。"""
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _metadata_path(target: Path) -> Path:
    return Path(str(target) + ".metadata.json")


def _pending_path(target: Path) -> Path:
    """归档提交记录在 tar 与正式元数据之间提供崩溃恢复依据。"""
    return Path(str(target) + ".pending.json")


def _write_json_atomic(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # 恢复清单的目录项必须先于小时包替换持久化，断电后才能继续提交。
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def publish_hour_archive(segments: Iterable[dict], target: Path) -> Path:
    """合并小时包；正文、索引与 pending 清单全部核验后才删除原始正文。"""
    target = Path(target)
    new_segments = [dict(segment) for segment in segments]
    metadata_path = _metadata_path(target)
    pending_path = _pending_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    previous_pending: list[dict] = []
    if pending_path.exists():
        with pending_path.open(encoding="utf-8") as source:
            previous_pending = list(json.load(source).get("segments") or [])
    pending_by_name = {str(item["logName"]): item for item in previous_pending}
    for segment in new_segments:
        name = str(segment["logName"])
        existing = pending_by_name.get(name)
        if existing is not None and (existing["sha256"], existing["rawSize"]) != (segment["sha256"], segment["rawSize"]):
            raise OSError("待恢复分卷名称与摘要冲突")
        pending_by_name[name] = segment
    all_segments = list(pending_by_name.values())
    # 先持久化恢复清单，之后任意 tar/metadata 发布中断都能由新会话继续完成。
    _write_json_atomic(pending_path, {"formatVersion": 1, "segments": all_segments})
    old_metadata = {"formatVersion": 2, "members": []}
    if metadata_path.exists():
        with metadata_path.open(encoding="utf-8") as source:
            old_metadata = json.load(source)
    old_members = list(old_metadata.get("members") or [])
    old_by_name = {str(member["logName"]): member for member in old_members}
    fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".partial", dir=target.parent)
    os.close(fd)
    partial = Path(name)
    try:
        existing_names: set[str] = set()
        with tarfile.open(partial, "w:gz", compresslevel=1) as bundle:
            if target.exists():
                with tarfile.open(target, "r:gz") as previous:
                    for member in previous.getmembers():
                        if not member.isfile() or not member.name.endswith(".log"):
                            raise OSError("已有小时归档包含非日志成员")
                        stream = previous.extractfile(member)
                        assert stream is not None
                        bundle.addfile(member, stream)
                        existing_names.add(member.name)
            for segment in all_segments:
                log = Path(segment["logPath"])
                index = target.parent / str(segment["indexName"])
                with index.open("rb") as source:
                    index_digest, index_size = _hash_stream(source)
                if (index_digest, index_size) != (segment["indexSha256"], segment["indexBytes"]):
                    raise OSError("原始索引摘要校验失败")
                if log.exists():
                    with log.open("rb") as source:
                        digest, size = _hash_stream(source)
                    if digest != segment["sha256"] or size != segment["rawSize"]:
                        raise OSError("原始日志摘要校验失败")
                elif segment["logName"] not in existing_names:
                    raise OSError("待恢复原始日志不存在")
                if segment["logName"] not in existing_names:
                    bundle.add(log, arcname=segment["logName"])
                    existing_names.add(segment["logName"])
        additions = [{key: value for key, value in segment.items() if key != "logPath"}
                     for segment in all_segments if str(segment["logName"]) not in old_by_name]
        # tar 已替换而元数据尚未替换时，原始分卷仍在。以成员名和摘要复核后把
        # 缺失的成员补进旁路清单，重试不会重复写入 tar。
        members = old_members + additions
        with tarfile.open(partial, "r:gz") as bundle:
            names = bundle.getnames()
            if len(names) != len(members) or set(names) != {str(item["logName"]) for item in members} or any(not name.endswith(".log") for name in names):
                raise OSError("小时归档成员不符合日志合同")
            for member in members:
                stream = bundle.extractfile(str(member["logName"]))
                if stream is None or _hash_stream(stream) != (member["sha256"], member["rawSize"]):
                    raise OSError("小时归档正文校验失败")
        with partial.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, target)
        _write_json_atomic(metadata_path, {"formatVersion": 2, "members": members})
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        for segment in all_segments:
            Path(segment["logPath"]).unlink(missing_ok=True)
        pending_path.unlink(missing_ok=True)
        return target
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def publish_archive(log: Path, index: Path, manifest: dict) -> Path:
    """兼容旧调用者的单分卷发布入口。"""
    target = log.with_suffix(".tar.gz")
    with index.open("rb") as source:
        index_hash, index_bytes = _hash_stream(source)
    return publish_hour_archive([manifest | {"logPath": str(log), "logName": log.name,
        "indexName": index.name, "indexSha256": index_hash, "indexBytes": index_bytes}], target)


async def compress(log: Path, index: Path, manifest: dict) -> Path:
    """兼容旧调用者，将单分卷交由唯一压缩进程处理。"""
    with index.open("rb") as source:
        index_hash, index_bytes = _hash_stream(source)
    return await compress_hour([manifest | {"logPath": str(log), "logName": log.name,
        "indexName": index.name, "indexSha256": index_hash, "indexBytes": index_bytes}], log.with_suffix(".tar.gz"))


async def compress_hour(segments: list[dict], target: Path) -> Path:
    """把同小时分卷交给唯一 spawn 进程，避免压缩争抢采集线程池。"""
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    return await asyncio.get_running_loop().run_in_executor(_pool, publish_hour_archive, segments, target)


async def shutdown_compression() -> None:
    """在节点收尾后等待压缩进程退出。"""
    global _pool
    if _pool is not None:
        pool, _pool = _pool, None
        await asyncio.to_thread(pool.shutdown, wait=True)
