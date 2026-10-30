"""独立进程合并小时日志包，并以旁路元数据保存索引与成员清单。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import multiprocessing
import os
import tarfile
import tempfile
import time
from collections import deque
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from camera_logs.logs.archive_spool import load as load_archive_spool
from camera_logs.logs.archive_spool import pending_path as _spool_pending_path
from camera_logs.logs.archive_spool import write_json_atomic as _write_json_atomic
from camera_logs.logs.background_execution import BackgroundJob
from camera_logs.logs.background_execution import archive_key as _archive_key
from camera_logs.logs.background_execution import log_lifecycle as _archive_log
from camera_logs.logs.background_execution import safe_archive_owner as _archive_owner
from camera_logs.logs.background_execution import split_background_budget as _split_background_budget
from camera_logs.logs.job_threads import job_thread
from camera_logs.logs.process_progress import advance as advance_progress
from camera_logs.logs.process_progress import create as create_progress
from camera_logs.logs.process_progress import install as install_progress
from camera_logs.logs.process_progress import snapshot as progress_snapshot
from camera_logs.logs.search_index import build_index

logger = logging.getLogger(__name__)

_pool: ProcessPoolExecutor | None = None
_pool_progress = None
_index_pool: ProcessPoolExecutor | None = None
_index_pool_progress = None
_dispatcher: asyncio.Task | None = None
_dispatcher_loop = None
_index_dispatcher: asyncio.Task | None = None
_index_dispatcher_loop = None
_closing = False
MAX_SCHEDULED_ARCHIVES = 8
RESERVED_RECOVERY_ARCHIVES = 2
MAX_SCHEDULED_INDEXES = 8
ARCHIVE_RETRY_BASE_SECONDS = 30
ARCHIVE_RETRY_MAX_SECONDS = 300
ARCHIVE_RETRY_MAX_EXPONENT = 4
MAX_ARCHIVE_RETRY_ENTRIES = 1024
PROCESS_PROGRESS_TIMEOUT_SECONDS = 300
PROCESS_TERMINATE_GRACE_SECONDS = 5
_scheduled_targets: set[Path] = set()
_scheduled_futures: dict[Path, asyncio.Future] = {}
_scheduled_index_targets: set[Path] = set()
_scheduled_index_futures: dict[Path, asyncio.Future] = {}
_retry_not_before: dict[Path, float] = {}
_retry_attempts: dict[Path, int] = {}
_archive_jobs: deque[BackgroundJob] = deque()
_index_jobs: deque[BackgroundJob] = deque()


def _pool_for_current_process() -> ProcessPoolExecutor:
    """延迟创建唯一压缩进程，避免归档与索引争抢采集写入线程池。"""
    global _pool, _pool_progress
    if _pool is None:
        context = multiprocessing.get_context("spawn")
        _pool_progress = create_progress(context)
        _pool = ProcessPoolExecutor(max_workers=1, mp_context=context, initializer=install_progress,
                                    initargs=(_pool_progress,))
    return _pool


def _index_pool_for_current_process() -> ProcessPoolExecutor:
    """延迟创建单槽索引进程；它与归档池隔离，不能占住新 spool 的发布槽位。"""
    global _index_pool, _index_pool_progress
    if _index_pool is None:
        context = multiprocessing.get_context("spawn")
        _index_pool_progress = create_progress(context)
        _index_pool = ProcessPoolExecutor(max_workers=1, mp_context=context, initializer=install_progress,
                                          initargs=(_index_pool_progress,))
    return _index_pool


async def _run_in_pool(function, *arguments):
    """提交已获父进程槽位的工作；子进程队列此时最多只有当前一项。"""
    return await _run_in_selected_pool(function, arguments, index=False)


async def _run_index_in_pool(function, *arguments):
    """提交低优先级索引；独立单槽只能消耗其固定读取预算。"""
    return await _run_in_selected_pool(function, arguments, index=True)


async def _run_in_selected_pool(function, arguments: tuple, *, index: bool):
    """在指定单槽池提交作业，池损坏只废弃自身，不影响另一类后台任务。"""
    pool = _index_pool_for_current_process() if index else _pool_for_current_process()
    progress = _index_pool_progress if index else _pool_progress
    try:
        submitted_at = time.monotonic()
        future = asyncio.get_running_loop().run_in_executor(pool, function, *arguments)
        return await _wait_future(future, progress=progress, pool=pool, submitted_at=submitted_at, index=index)
    except BrokenProcessPool:
        # 当前作业的 tar 发布状态不可判定，不能整批重试；只废弃已损坏的执行器，
        # 让后续仍由 spool/pending 保证可恢复的作业在新进程中重新提交。
        await _discard_broken_pool(pool, index=index)
        raise


async def _discard_broken_pool(pool: ProcessPoolExecutor, *, index: bool = False) -> None:
    """同步回收异常退出的进程池，避免全局引用让后续作业持续提交失败。"""
    global _pool, _pool_progress, _index_pool, _index_pool_progress
    await job_thread(pool.shutdown, wait=True, cancel_futures=True)
    if index and _index_pool is pool:
        _index_pool = None
        _index_pool_progress = None
    elif not index and _pool is pool:
        _pool = None
        _pool_progress = None


def _start_dispatcher() -> None:
    """在当前事件循环按归档优先顺序启动唯一调度协程。"""
    global _dispatcher, _dispatcher_loop
    if _closing:
        return
    loop = asyncio.get_running_loop()
    if _dispatcher is not None and not _dispatcher.done():
        if _dispatcher_loop is not loop:
            raise RuntimeError("压缩调度器仍属于旧事件循环")
        return
    _dispatcher_loop = loop
    _dispatcher = loop.create_task(_dispatch_jobs())


def _start_index_dispatcher() -> None:
    """只有归档队列和归档执行都空闲时才启动低优先级索引；运行后与归档互不阻塞。"""
    global _index_dispatcher, _index_dispatcher_loop
    if _closing:
        return
    loop = asyncio.get_running_loop()
    if _index_dispatcher is not None and not _index_dispatcher.done():
        if _index_dispatcher_loop is not loop:
            raise RuntimeError("索引调度器仍属于旧事件循环")
        return
    if _archive_jobs or (_dispatcher is not None and not _dispatcher.done()):
        return
    _index_dispatcher_loop = loop
    _index_dispatcher = loop.create_task(_dispatch_index_jobs())


def _enqueue(kind: str, *arguments, token: object | None = None,
             owner: tuple[str, str, str] = ("-", "-", "-"), callback=None) -> asyncio.Future:
    """登记工作而不立即提交进程池，避免索引先入 FIFO 后阻塞待归档小时。"""
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    job = BackgroundJob(kind, arguments, result, time.monotonic(), _archive_key(kind, arguments),
               _archive_owner(arguments[0]) if kind == "archive" and arguments else owner,
               token=token, callback=callback)
    if _closing:
        if kind in {"archive", "spool", "index"}:
            _archive_log(job, "CANCELLED")
        result.cancel()
        if callback is not None:
            callback(asyncio.CancelledError())
        return result
    (_archive_jobs if kind in {"archive", "spool"} else _index_jobs).append(job)
    if kind in {"archive", "spool", "index"}:
        _archive_log(job, "QUEUED")
    _start_dispatcher()
    if kind == "index":
        _start_index_dispatcher()
    return result


async def _dispatch_jobs() -> None:
    """归档单槽选择器；索引已由独立低优先级执行器隔离。"""
    global _dispatcher
    try:
        while _archive_jobs:
            job = _archive_jobs.popleft()
            started_at = None
            try:
                if job.kind in {"archive", "spool"}:
                    started_at = time.monotonic()
                    _archive_log(job, "RUNNING", started_at=started_at)
                    from camera_logs.node import performance_runtime

                    rate, allow_index = performance_runtime.background_settings()
                    # 并行索引始终只使用另一份 20% 预算，归档固定使用 80%。
                    # 内联索引会占归档进程，必须禁用，缺索引时搜索仍回退原文。
                    background_rate, _index_rate = _split_background_budget(rate)
                    create_index = False
                    running = getattr(performance_runtime, "archive_running", None)
                    if running is not None:
                        running(job.token)
                    publisher = publish_spooled_archive if job.kind == "spool" else publish_hour_archive
                    value = await _run_in_pool(publisher, *job.arguments, background_rate, create_index)
            except asyncio.CancelledError:
                if job.kind in {"archive", "spool"}:
                    _archive_log(job, "CANCELLED", started_at=started_at)
                if not job.result.done():
                    job.result.cancel()
                if job.callback is not None:
                    job.callback(asyncio.CancelledError())
                raise
            except Exception as error:  # noqa: BLE001 - 子进程故障只结算当前作业，后续归档仍可执行。
                retry_after = job.callback(error) if job.callback is not None else 0
                if job.kind in {"archive", "spool"}:
                    _archive_log(job, "FAILED", started_at=started_at, error=error,
                                 retry_after_seconds=retry_after or 0)
                if not job.result.done():
                    job.result.set_exception(error)
            else:
                if job.kind in {"archive", "spool"}:
                    _archive_log(job, "SUCCEEDED", started_at=started_at)
                    # 已验证发布后只登记有界低优先级索引，不等待它完成；压力
                    # 策略禁用时由持久目录维护在后续空闲周期补建。
                    if allow_index and isinstance(value, Path):
                        schedule_published_index(value)
                if not job.result.done():
                    job.result.set_result(value)
                if job.callback is not None:
                    job.callback(None)
    finally:
        if _dispatcher is asyncio.current_task():
            _dispatcher = None
        for job in _archive_jobs:
            if job.kind in {"archive", "spool"}:
                _archive_log(job, "CANCELLED")
            if not job.result.done():
                job.result.cancel()
            if job.callback is not None:
                try:
                    # 排队项从未进入主循环，必须在此结算持久 spool 的身份、重试
                    # 状态和性能 token；否则下一轮恢复会误判队列仍满。
                    job.callback(asyncio.CancelledError())
                except Exception:
                    logger.exception("取消排队后台作业的收尾失败")
        _archive_jobs.clear()
        _start_index_dispatcher()


async def _dispatch_index_jobs() -> None:
    """索引单槽选择器；已运行索引不能阻塞随后到达的归档作业。"""
    global _index_dispatcher
    try:
        while _index_jobs:
            job = _index_jobs.popleft()
            started_at = None
            try:
                from camera_logs.node import performance_runtime

                rate, allow_index = performance_runtime.background_settings()
                # 已运行的索引不强杀；但每个尚未提交子进程的候选都必须重新
                # 让位给最新压力策略和归档队列。被放弃的候选由目录维护重试。
                if not allow_index:
                    _defer_index_job(job, "INDEX_DEFERRED_BY_PRESSURE")
                    continue
                if _archive_jobs or (_dispatcher is not None and not _dispatcher.done()):
                    _defer_index_job(job, "INDEX_DEFERRED_TO_ARCHIVE")
                    continue
                _archive_rate, index_rate = _split_background_budget(rate)
                started_at = time.monotonic()
                _archive_log(job, "RUNNING", started_at=started_at)
                value = await _run_index_in_pool(build_index, *job.arguments, index_rate)
            except asyncio.CancelledError:
                _archive_log(job, "CANCELLED", started_at=started_at)
                if not job.result.done():
                    job.result.cancel()
                if job.callback is not None:
                    job.callback(asyncio.CancelledError())
                raise
            except Exception as error:  # noqa: BLE001 - 单项索引失败不阻止其余补建。
                _archive_log(job, "FAILED", started_at=started_at, error=error)
                if not job.result.done():
                    job.result.set_exception(error)
                if job.callback is not None:
                    job.callback(error)
            else:
                if value is False:
                    _archive_log(job, "INCOMPLETE", started_at=started_at,
                                 error_type="INDEX_NOT_PUBLISHED")
                else:
                    _archive_log(job, "SUCCEEDED", started_at=started_at)
                if not job.result.done():
                    job.result.set_result(value)
                if job.callback is not None:
                    job.callback(None)
    finally:
        if _index_dispatcher is asyncio.current_task():
            _index_dispatcher = None
        for job in _index_jobs:
            _archive_log(job, "CANCELLED")
            if not job.result.done():
                job.result.cancel()
            if job.callback is not None:
                try:
                    job.callback(asyncio.CancelledError())
                except Exception:
                    logger.exception("取消排队索引作业的收尾失败")
        _index_jobs.clear()


def _defer_index_job(job: BackgroundJob, reason: str) -> None:
    """尚未运行的索引让位后结算为未发布，清除去重身份以便维护安全补建。"""
    _archive_log(job, "INCOMPLETE", error_type=reason)
    if not job.result.done():
        job.result.set_result(False)
    _release_scheduled_index(Path(job.arguments[0]), job.result)
    if job.callback is not None:
        job.callback(None)


async def _wait_future(future: asyncio.Future, *, progress=None, pool=None, submitted_at=0.0, index=False):
    """等待不可取消底层作业；后台失败不能覆盖已收到的调用方取消。"""
    cancelled = False
    while not future.done():
        try:
            if progress is None:
                await asyncio.shield(future)
            else:
                await asyncio.wait_for(asyncio.shield(future), timeout=1)
        except TimeoutError:
            if progress is None:
                break
            stamp, processed, phase = progress_snapshot(progress)
            if time.monotonic() - max(submitted_at, stamp) < PROCESS_PROGRESS_TIMEOUT_SECONDS:
                continue
            logger.error("后台%s进程无进展，终止执行器 phase=%s processedBytes=%s",
                         "索引" if index else "归档", phase, processed)
            await job_thread(_terminate_pool, pool)
            global _pool, _pool_progress, _index_pool, _index_pool_progress
            if index and _index_pool is pool:
                _index_pool = _index_pool_progress = None
            elif not index and _pool is pool:
                _pool = _pool_progress = None
            await asyncio.gather(future, return_exceptions=True)
            raise TimeoutError("后台压缩进程持续无进展")
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
    """关闭两类进程池前排空归档与索引队列，避免任一类遗留子进程。"""
    cancelled = False
    while _dispatcher is not None or _index_dispatcher is not None:
        tasks = tuple(task for task in (_dispatcher, _index_dispatcher) if task is not None)
        try:
            await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


def _cancel_queued_jobs() -> None:
    """超时关闭时结算未启动项；关闭门闩期间不得再由 archive finally 拉起索引。"""
    for jobs in (_archive_jobs, _index_jobs):
        while jobs:
            job = jobs.popleft()
            _archive_log(job, "CANCELLED")
            if not job.result.done():
                job.result.cancel()
            if job.callback is not None:
                try:
                    job.callback(asyncio.CancelledError())
                except Exception:
                    logger.exception("取消关闭中排队后台作业的收尾失败")


def _hash_stream(stream, limiter=None):
    """分块计算摘要和字节数，避免大文件在压缩进程中占用整块内存。"""
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1024 * 1024):
        if limiter:
            limiter.consume(len(chunk))
        digest.update(chunk)
        size += len(chunk)
        advance_progress("HASH", len(chunk))
    return digest.hexdigest(), size


class _ProgressReader:
    """为 tar 的 gzip 元数据遍历补充真实读取进度，避免 getmembers 长读被误判卡死。"""
    def __init__(self, source, phase: str):
        self.source, self.phase = source, phase

    def read(self, size=-1):
        data = self.source.read(size)
        advance_progress(self.phase, len(data))
        return data

    def __getattr__(self, name):
        return getattr(self.source, name)


def _metadata_path(target: Path) -> Path:
    return Path(str(target) + ".metadata.json")


def _pending_path(target: Path) -> Path:
    """归档提交记录在 tar 与正式元数据之间提供崩溃恢复依据。"""
    return _spool_pending_path(target)


def publish_hour_archive(segments: Iterable[dict], target: Path, background_rate: int | None = None,
                         create_index: bool = True) -> Path:
    """合并小时包；正文、索引与 pending 清单全部核验后才删除原始正文。"""
    target = Path(target)
    from camera_logs.logs.archive_access import LimitedReader, ReadLimiter, gzip_tar_writer
    limiter = ReadLimiter(background_rate) if background_rate else None

    class ArchiveReader(LimitedReader):
        """仅在节点持续压力时节流压缩输入；独立于查询进程的读取预算。"""
        def read(self, size=-1):
            data = self.source.read(self.remaining if size < 0 else min(size, self.remaining))
            self.remaining -= len(data)
            if limiter:
                limiter.consume(len(data))
            advance_progress("TAR", len(data))
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
        with partial.open("wb") as target_file, gzip_tar_writer(target_file) as bundle:
            if target.exists():
                with target.open("rb") as source, tarfile.open(fileobj=_ProgressReader(source, "TAR"), mode="r|gz") as previous:
                    for member in previous:
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
                if log.exists() and segment["logName"] in existing_names:
                    with log.open("rb") as source:
                        digest, size = _hash_stream(source, limiter)
                    if digest != segment["sha256"] or size != segment["rawSize"]:
                        raise OSError("原始日志摘要校验失败")
                elif not log.exists() and segment["logName"] not in existing_names:
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
        # 单遍顺序解压并逐成员完整SHA核验，不先getnames扫描一次再回卷解压。
        expected = {str(member["logName"]): member for member in members}
        if len(expected) != len(members):
            raise OSError("小时归档元数据包含重复成员")
        checked = set()
        with partial.open("rb") as source, tarfile.open(fileobj=_ProgressReader(source, "VERIFY"), mode="r|gz") as bundle:
            for info in bundle:
                member = expected.get(info.name)
                if not info.isfile() or member is None or info.name in checked or not info.name.endswith(".log"):
                    raise OSError("小时归档成员不符合日志合同")
                stream = bundle.extractfile(info)
                if stream is None or _hash_stream(stream, limiter) != (member["sha256"], member["rawSize"]):
                    raise OSError("小时归档正文校验失败")
                checked.add(info.name)
        if checked != set(expected):
            raise OSError("小时归档成员缺失")
        with partial.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, target)
        _write_json_atomic(metadata_path, {"formatVersion": 2, "members": members})
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        from camera_logs.logs.archive_receipts import persist_verified
        persist_verified(target, members)
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


def publish_spooled_archive(spool: Path, background_rate: int | None = None, create_index: bool = True) -> Path:
    """消费一个不可变封存 spool；成功发布后才删除该 spool。"""
    spool = Path(spool)
    target, segments = load_archive_spool(spool)
    result = publish_hour_archive(segments, target, background_rate, create_index)
    spool.unlink(missing_ok=True)
    return result


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


def _schedule_archive(kind: str, identity: Path, *arguments: object,
                      owner: tuple[str, str, str] = ("-", "-", "-"),
                      admission_limit: int = MAX_SCHEDULED_ARCHIVES) -> asyncio.Future | None:
    """将一个可恢复归档公平登记为有限候选，失败项退避后才允许下轮重试。"""
    identity = Path(identity)
    exists = identity.exists() if kind == "spool" else (identity.exists() or _pending_path(identity).exists())
    if not exists:
        _retry_not_before.pop(identity, None)
        _retry_attempts.pop(identity, None)
        return None
    if identity in _scheduled_targets:
        return _scheduled_futures[identity]
    if time.monotonic() < _retry_not_before.get(identity, 0):
        return None
    if len(_scheduled_targets) >= admission_limit:
        return None
    from camera_logs.node.performance_runtime import archive_finished, archive_started

    token = object()
    archive_started(token, persisted=kind in {"spool", "archive"})
    _scheduled_targets.add(identity)

    def complete(error: BaseException | None) -> int:
        _scheduled_targets.discard(identity)
        _scheduled_futures.pop(identity, None)
        if error is None:
            _retry_not_before.pop(identity, None)
            _retry_attempts.pop(identity, None)
            delay = 0
        elif isinstance(error, asyncio.CancelledError):
            # Worker 正常关闭时 spool 留给下次维护重新发现；取消不属于发布失败，
            # 不能写错误或制造退避窗口。
            delay = 0
        else:
            attempts = min(_retry_attempts.get(identity, 0) + 1, ARCHIVE_RETRY_MAX_EXPONENT + 1)
            _retry_attempts[identity] = attempts
            delay = min(ARCHIVE_RETRY_BASE_SECONDS * 2 ** min(attempts - 1, ARCHIVE_RETRY_MAX_EXPONENT),
                        ARCHIVE_RETRY_MAX_SECONDS)
            if identity not in _retry_not_before and len(_retry_not_before) >= MAX_ARCHIVE_RETRY_ENTRIES:
                oldest = next(iter(_retry_not_before))
                _retry_not_before.pop(oldest, None)
                _retry_attempts.pop(oldest, None)
            _retry_not_before[identity] = time.monotonic() + delay
        archive_finished(token)
        return delay

    future = _enqueue(kind, *arguments, token=token, owner=owner, callback=complete)
    _scheduled_futures[identity] = future
    # 调度调用方不等待结果；回调已读取异常，避免未观察的 Future 告警。
    future.add_done_callback(lambda value: value.exception() if not value.cancelled() else None)
    return future


def schedule_pending_archive(target: Path, *, recovery: bool | None = None,
                             owner: tuple[str, str, str] = ("-", "-", "-")) -> asyncio.Future | None:
    """登记持久 spool；实时 Writer 显式传 False，缺省兼容旧维护调用的全额容量。"""
    spool = Path(target)
    limit = MAX_SCHEDULED_ARCHIVES if recovery is not False else MAX_SCHEDULED_ARCHIVES - RESERVED_RECOVERY_ARCHIVES
    return _schedule_archive("spool", spool, spool, owner=owner, admission_limit=limit)


def schedule_recovery_archive(target: Path) -> asyncio.Future | None:
    """登记旧共享 pending 清单的恢复，不在节点启动路径等待压缩完成。"""
    archive = Path(target)
    return _schedule_archive("archive", archive, [], archive, admission_limit=MAX_SCHEDULED_ARCHIVES)


async def compress_spool(spool: Path) -> Path:
    """同步等待单个已落盘 spool，供显式关闭和启动恢复保持既有完成合同。"""
    from camera_logs.node.performance_runtime import archive_finished, archive_started

    token = object()
    archive_started(token, persisted=True)
    try:
        return await _wait_for_job(_enqueue("spool", Path(spool), token=token))
    finally:
        archive_finished(token)


async def build_search_index(target: Path) -> bool:
    """低优先级补建入口；压力、重复或队满时返回 false，由后续维护重试。"""
    future = schedule_published_index(target)
    return await _wait_for_job(future) if future is not None else False


def schedule_published_index(target: Path) -> asyncio.Future | None:
    """为 READY 小时包有界登记独立索引，按目标去重并观察后台异常。"""
    target = Path(target)
    from camera_logs.node import performance_runtime

    _rate, allow_index = performance_runtime.background_settings()
    if not allow_index or _closing:
        return None
    existing = _scheduled_index_futures.get(target)
    if existing is not None:
        return existing
    if len(_scheduled_index_targets) >= MAX_SCHEDULED_INDEXES:
        return None
    future = _enqueue("index", target)
    if future.cancelled():
        return None
    _scheduled_index_targets.add(target)
    _scheduled_index_futures[target] = future

    def completed(done: asyncio.Future) -> None:
        _release_scheduled_index(target, done)
        # 自动登记者不 await；主动读取失败可避免未观察 Future，同时不改变
        # 调用 build_search_index 的 await 语义。
        if not done.cancelled():
            try:
                done.exception()
            except asyncio.CancelledError:
                pass

    future.add_done_callback(completed)
    return future


def _release_scheduled_index(target: Path, future: asyncio.Future) -> None:
    """仅释放仍指向当前 Future 的去重身份，迟到回调不能删除后来同 target 的重建。"""
    if _scheduled_index_futures.get(target) is future:
        _scheduled_index_futures.pop(target, None)
        _scheduled_index_targets.discard(target)


def _terminate_pool(pool: ProcessPoolExecutor) -> None:
    """超时关闭时终止唯一压缩子进程，不能留下它继续改写新 Worker 的小时包。"""
    for process in tuple(getattr(pool, "_processes", {}).values()):
        if process.is_alive():
            process.terminate()
    deadline = time.monotonic() + PROCESS_TERMINATE_GRACE_SECONDS
    for process in tuple(getattr(pool, "_processes", {}).values()):
        process.join(max(0, deadline - time.monotonic()))
        if process.is_alive():
            process.kill()
    for process in tuple(getattr(pool, "_processes", {}).values()):
        process.join(PROCESS_TERMINATE_GRACE_SECONDS)
        if process.is_alive():
            raise RuntimeError("压缩子进程在 kill 后仍未退出，拒绝启动新的写入进程")
    pool.shutdown(wait=True, cancel_futures=True)


async def shutdown_compression(*, timeout_seconds: float | None = None) -> None:
    """收尾归档与索引执行器；超时后分别终止，绝不让旧进程跨 Worker 存活。"""
    global _pool, _pool_progress, _index_pool, _index_pool_progress, _closing
    cancelled = False
    try:
        try:
            if timeout_seconds is None:
                await _drain_dispatcher()
            else:
                await asyncio.wait_for(asyncio.shield(_drain_dispatcher()), timeout_seconds)
        except TimeoutError:
            # 等待层刻意忽略取消直到不可取消进程工作结束；先分别杀掉两个
            # 子进程，再等待 future 结算，避免旧 Worker 的归档或索引持续运行。
            _closing = True
            if _dispatcher is not None:
                _dispatcher.cancel()
            if _index_dispatcher is not None:
                _index_dispatcher.cancel()
            if _pool is not None:
                pool = _pool
                await job_thread(_terminate_pool, pool)
                if _pool is pool:
                    _pool = None
                    _pool_progress = None
            if _index_pool is not None:
                pool = _index_pool
                await job_thread(_terminate_pool, pool)
                if _index_pool is pool:
                    _index_pool = None
                    _index_pool_progress = None
            await _drain_dispatcher()
            _cancel_queued_jobs()
        except asyncio.CancelledError:
            cancelled = True
        if _pool is not None:
            pool = _pool
            try:
                await job_thread(pool.shutdown, wait=True)
            finally:
                if _pool is pool:
                    _pool = None
                    _pool_progress = None
        if _index_pool is not None:
            pool = _index_pool
            try:
                await job_thread(pool.shutdown, wait=True)
            finally:
                if _index_pool is pool:
                    _index_pool = None
                    _index_pool_progress = None
    finally:
        _closing = False
    if cancelled:
        raise asyncio.CancelledError
