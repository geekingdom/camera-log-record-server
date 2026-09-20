"""独立进程合并小时日志包，并以旁路元数据保存索引与成员清单。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import tarfile
import tempfile
from collections import deque
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from camera_logs.logs.search_index import build_index

_pool: ProcessPoolExecutor | None = None
_dispatcher: asyncio.Task | None = None
_dispatcher_loop = None


@dataclass(slots=True)
class _Job:
    """父进程中的单槽候选；只有被选中时才向不可重排的进程池提交。"""

    kind: str
    arguments: tuple
    result: asyncio.Future
    token: object | None = None


_archive_jobs: deque[_Job] = deque()
_index_jobs: deque[_Job] = deque()


def _pool_for_current_process() -> ProcessPoolExecutor:
    """延迟创建唯一压缩进程，避免归档与索引争抢采集写入线程池。"""
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    return _pool


async def _run_in_pool(function, *arguments):
    """提交已获父进程槽位的工作；子进程队列此时最多只有当前一项。"""
    future = asyncio.get_running_loop().run_in_executor(_pool_for_current_process(), function, *arguments)
    return await _wait_future(future)


def _start_dispatcher() -> None:
    """在当前事件循环按归档优先顺序启动唯一调度协程。"""
    global _dispatcher, _dispatcher_loop
    loop = asyncio.get_running_loop()
    if _dispatcher is not None and not _dispatcher.done():
        if _dispatcher_loop is not loop:
            raise RuntimeError("压缩调度器仍属于旧事件循环")
        return
    _dispatcher_loop = loop
    _dispatcher = loop.create_task(_dispatch_jobs())


def _enqueue(kind: str, *arguments, token: object | None = None) -> asyncio.Future:
    """登记工作而不立即提交进程池，避免索引先入 FIFO 后阻塞待归档小时。"""
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    job = _Job(kind, arguments, result, token)
    (_archive_jobs if kind == "archive" else _index_jobs).append(job)
    _start_dispatcher()
    return result


async def _dispatch_jobs() -> None:
    """单槽选择器：所有已等待归档优先，索引只在归档队列清空后才允许执行。"""
    global _dispatcher
    try:
        while _archive_jobs or _index_jobs:
            job = _archive_jobs.popleft() if _archive_jobs else _index_jobs.popleft()
            try:
                if job.kind == "archive":
                    from camera_logs.node import performance_runtime

                    rate, allow_index = performance_runtime.background_settings()
                    # 正常无积压时保留内联索引吞吐；压力或后续归档等待时先发布 READY。
                    create_index = allow_index and not _archive_jobs
                    background_rate = None if allow_index else rate
                    running = getattr(performance_runtime, "archive_running", None)
                    if running is not None:
                        running(job.token)
                    value = await _run_in_pool(publish_hour_archive, *job.arguments, background_rate, create_index)
                else:
                    value = await _run_in_pool(build_index, *job.arguments)
            except asyncio.CancelledError:
                if not job.result.done():
                    job.result.cancel()
                raise
            except Exception as error:  # noqa: BLE001 - 子进程故障只结算当前作业，后续归档仍可执行。
                if not job.result.done():
                    job.result.set_exception(error)
            else:
                if not job.result.done():
                    job.result.set_result(value)
    finally:
        if _dispatcher is asyncio.current_task():
            _dispatcher = None
        for job in (*_archive_jobs, *_index_jobs):
            if not job.result.done():
                job.result.cancel()
        _archive_jobs.clear()
        _index_jobs.clear()


async def _wait_future(future: asyncio.Future):
    """等待不可取消底层作业；后台失败不能覆盖已收到的调用方取消。"""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:  # noqa: BLE001 - 完成态由 future.result 或取消优先级统一处理。
            break
    if cancelled:
        if not future.cancelled():
            try:
                future.exception()
            except asyncio.CancelledError:
                pass
        raise asyncio.CancelledError
    return future.result()


async def _wait_for_job(future: asyncio.Future):
    """调用方取消时仍等待已登记作业的物理收尾，不能让槽位或临时文件失去所有者。"""
    return await _wait_future(future)


async def _drain_dispatcher() -> None:
    """关闭进程池前先排空父进程等待队列，避免已登记索引被静默遗弃。"""
    cancelled = False
    while _dispatcher is not None:
        try:
            await asyncio.shield(_dispatcher)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


def _hash_stream(stream, limiter=None):
    """分块计算摘要和字节数，避免大文件在压缩进程中占用整块内存。"""
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1024 * 1024):
        if limiter:
            limiter.consume(len(chunk))
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


def publish_hour_archive(segments: Iterable[dict], target: Path, background_rate: int | None = None,
                         create_index: bool = True) -> Path:
    """合并小时包；正文、索引与 pending 清单全部核验后才删除原始正文。"""
    target = Path(target)
    from camera_logs.logs.archive_access import LimitedReader, ReadLimiter
    limiter = ReadLimiter(background_rate) if background_rate else None

    class ArchiveReader(LimitedReader):
        """仅在节点持续压力时节流压缩输入；独立于查询进程的读取预算。"""
        def read(self, size=-1):
            data = self.source.read(self.remaining if size < 0 else min(size, self.remaining))
            self.remaining -= len(data)
            if limiter:
                limiter.consume(len(data))
            return data
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
                        bundle.addfile(member, ArchiveReader(stream, member.size))
                        existing_names.add(member.name)
            for segment in all_segments:
                log = Path(segment["logPath"])
                index = target.parent / str(segment["indexName"])
                with index.open("rb") as source:
                    index_digest, index_size = _hash_stream(source, limiter)
                if (index_digest, index_size) != (segment["indexSha256"], segment["indexBytes"]):
                    raise OSError("原始索引摘要校验失败")
                if log.exists():
                    with log.open("rb") as source:
                        digest, size = _hash_stream(source, limiter)
                    if digest != segment["sha256"] or size != segment["rawSize"]:
                        raise OSError("原始日志摘要校验失败")
                elif segment["logName"] not in existing_names:
                    raise OSError("待恢复原始日志不存在")
                if segment["logName"] not in existing_names:
                    info = bundle.gettarinfo(str(log), arcname=segment["logName"])
                    with log.open("rb") as source:
                        bundle.addfile(info, ArchiveReader(source, info.size))
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
                if stream is None or _hash_stream(stream, limiter) != (member["sha256"], member["rawSize"]):
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
        # 索引只是搜索候选加速器；SQLite/磁盘故障绝不能撤销已验证发布的小时包。
        if create_index:
            build_index(target)
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
    """登记小时归档；实际执行时读取最新预算并优先于所有索引补建。"""
    from camera_logs.node.performance_runtime import archive_finished, archive_started

    token = object()
    archive_started(token)
    try:
        future = _enqueue("archive", segments, target, token=token)
        return await _wait_for_job(future)
    finally:
        archive_finished(token)


async def build_search_index(target: Path) -> bool:
    """低优先级补建旧小时包索引；有待归档小时时先让归档解除积压。"""
    return await _wait_for_job(_enqueue("index", Path(target)))


async def shutdown_compression() -> None:
    """在节点收尾后等待压缩进程退出。"""
    global _pool
    cancelled = False
    try:
        await _drain_dispatcher()
    except asyncio.CancelledError:
        cancelled = True
    if _pool is not None:
        from camera_logs.logs.job_threads import job_thread
        pool = _pool
        try:
            await job_thread(pool.shutdown, wait=True)
        finally:
            if _pool is pool:
                _pool = None
    if cancelled:
        raise asyncio.CancelledError
