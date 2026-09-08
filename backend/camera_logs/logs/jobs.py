"""节点侧的受限导出和字面关键词搜索作业。

文件只在鉴权后的节点内解析，跨节点通过内部接口流式取回；当前小时按创建
作业时冻结的水位截取。并发数、读带宽及产物大小受限，失败和取消持久化回报。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tarfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from camera_logs.logs.archive_access import LimitedReader, copy_limited, read_limiter, snapshot
from camera_logs.logs.export_output import write_zip
from camera_logs.logs.hour_download import (
    Source,
    hour_export_name,
    pin_hour_sources,
    reusable_hour_archive,
    write_hour_archive,
)
from camera_logs.logs.job_threads import job_thread
from camera_logs.logs.naming import safe_filename_component
from camera_logs.logs.search_stream import StreamSearch, index_entries

OUTPUT_LIMIT = 20_000_000_000
TEMP_LIMIT = 100_000_000_000
SEARCH_SNAPSHOT_LIMIT = 20_000_000_000
_jobs = asyncio.Semaphore(2)
_temp_reservation_lock = asyncio.Lock()
_temp_reservations: dict[str, int] = {}
_log = logging.getLogger(__name__)


def _root(repo: Any) -> Path:
    return Path(repo.settings.log_root).resolve()


def _contained(repo: Any, path: str | Path) -> Path:
    """规范化后验证路径归属，拒绝越过本节点日志根目录的文件访问。"""
    candidate = Path(path)
    candidate = candidate.resolve() if candidate.is_absolute() else (_root(repo) / candidate).resolve()
    if candidate != _root(repo) and _root(repo) not in candidate.parents:
        raise ValueError("file path is outside LOG_ROOT")
    return candidate


def _file_path(repo: Any, file: dict[str, Any]) -> Path:
    return _contained(repo, file.get("path") or file.get("rawPath") or "")


async def _file_doc(repo: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    current = await repo.db.files.find_one({"id": snapshot["id"]})
    if not current or current.get("status") not in ("OPEN", "READY"):
        raise FileNotFoundError(snapshot["id"])
    return current


async def _remote_archive(repo: Any, file: dict[str, Any], frozen: dict[str, Any], target: Path, include_index: bool = False, max_output_bytes: int | None = None) -> Path:
    """跨节点取回冻结快照，并限制读取速率和临时文件大小。"""
    node = await repo.get("nodes", file["nodeId"])
    params = {"includeIndex": "true"} if include_index else {}
    if frozen.get("status") != "READY":
        params["bytes"] = frozen.get("bytes", file.get("bytes", 0))
    written = 0
    async with httpx.AsyncClient(timeout=120) as client, client.stream("GET", node["url"] + f"/internal/archive/{file['id']}", params=params, headers={"Authorization": "Bearer " + repo.settings.internal_token}) as response:
        response.raise_for_status()
        with target.open("wb") as output:
            async for chunk in response.aiter_bytes(1024 * 1024):
                written += len(chunk)
                if written > (TEMP_LIMIT if max_output_bytes is None else max_output_bytes):
                    raise ValueError("snapshot storage limit exceeded")
                await asyncio.to_thread(read_limiter.consume, len(chunk))
                output.write(chunk)
    return target


def _archive_name(file: dict[str, Any], fallback_id: str, *, use_path: bool = True) -> str:
    """从文件元数据或当前路径取得可读归档名，旧目录记录回退到 file ID。"""
    metadata_name = file.get("archiveName") or file.get("rawFileName")
    if metadata_name:
        name = str(metadata_name)
    else:
        path = (file.get("path") or file.get("rawPath")) if use_path else None
        name = Path(str(path)).name if path else f"{fallback_id}.tar.gz"
    if name in {"", ".", ".."}:
        name = f"{fallback_id}.tar.gz"
    stem = name.removesuffix(".tar.gz").removesuffix(".log")
    return safe_filename_component(stem, max_utf8_bytes=180, fallback=fallback_id) + ".tar.gz"


def _unique_scratch_name(name: str, used: set[str]) -> str:
    """保留可读 basename，并为同一作业内的快照冲突追加稳定序号。"""
    suffix = "".join(Path(name).suffixes)
    stem = name.removesuffix(suffix)
    candidate, number = name, 2
    while candidate in used:
        candidate = f"{stem}-{number}{suffix}"
        number += 1
    used.add(candidate)
    return candidate


def _directory_size(path: Path) -> int:
    """统计临时根目录中现有常规文件字节数，忽略并发删除造成的竞态。"""
    if not path.exists():
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _estimated_temp_bytes(files: list[dict[str, Any]]) -> int:
    """按冻结水位估计新作业的临时空间，未知值按零处理但不影响已有占用检查。"""
    total = 0
    for file in files:
        try:
            total += max(0, int(file.get("bytes", 0)))
        except (TypeError, ValueError):
            continue
    return total


async def _reserve_temp(repo: Any, job_id: str, estimate: int) -> None:
    """在创建作业临时目录前保留节点级空间，避免并发导出同时越过配额。"""
    temporary_root = _root(repo) / "exports" / ".tmp"
    async with _temp_reservation_lock:
        existing = await asyncio.to_thread(_directory_size, temporary_root)
        if existing + sum(_temp_reservations.values()) + estimate > TEMP_LIMIT:
            raise ValueError("node temporary export storage limit exceeded")
        _temp_reservations[job_id] = estimate


async def _release_temp(job_id: str) -> None:
    """文件线程和清理均结束后，在事件循环内无等待地释放配额。

    准入锁用于串行化扫描和新增预留；删除只降低用量，不会造成超额准入，
    因此不能再等待该锁，让第二次取消绕过已完成作业的配额释放。
    """
    _temp_reservations.pop(job_id, None)


async def _archive(
    repo: Any,
    frozen: dict[str, Any],
    scratch: Path,
    scratch_name: str | None = None,
    include_index: bool = False,
    max_output_bytes: int | None = None,
) -> tuple[Path, bool]:
    """查找已封存归档或建立固定水位快照，返回路径及是否为临时文件。"""
    file = await _file_doc(repo, frozen)
    local = file.get("nodeId") == repo.settings.node_id
    target = scratch / (scratch_name or _archive_name(file, str(file["id"]), use_path=local))
    if not local:
        if include_index:
            await _remote_archive(repo, file, frozen, target, True, max_output_bytes)
        else:
            await _remote_archive(repo, file, frozen, target)
        return target, True
    path = _file_path(repo, file)
    if frozen.get("status") == "READY" and path.suffixes[-2:] == [".tar", ".gz"] and not include_index:
        return path, False
    index = _contained(repo, file["indexPath"]) if file.get("indexPath") else None
    await job_thread(snapshot, path, target, int(frozen.get("bytes", file.get("bytes", 0))), index, file["id"], file.get("archiveMember"), include_index, max_output_bytes=max_output_bytes)
    return target, True


async def _cancelled(repo: Any, identifier: str) -> bool:
    current = await repo.db.jobs.find_one({"id": identifier}, {"status": 1})
    return bool(current and current.get("status") == "CANCELLED")


def _expired(job: dict[str, Any]) -> bool:
    value = job.get("expiresAt")
    if value is None:
        return False
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return stamp.astimezone(UTC) <= datetime.now(UTC)


class JobProgress:
    """按冻结片段推进作业进度，避免高频扫描或复制逐块写入数据库。

    冻结字节数可用时以字节为权重；全部为零时按文件数平均推进。运行中最大
    只报告 99，最终的 100 与 SUCCEEDED 在同一次条件更新中提交，失败和取消
    因而不会被错误地显示为完成。
    """

    def __init__(self, repo: Any, job: dict[str, Any]) -> None:
        self.repo, self.job = repo, job
        files = job.get("files", [])
        weights = [max(0, int(file.get("bytes", 0))) for file in files]
        self.weights = weights if sum(weights) else [1] * len(files)
        self.total = sum(self.weights)
        self.completed = self.position = 0
        self.persisted = int(job.get("progress", 0))

    async def advance(self) -> None:
        """在一个冻结片段已处理后更新，百分点未变化时不额外落库。"""
        if self.position >= len(self.weights) or self.total <= 0:
            return
        self.completed += self.weights[self.position]
        self.position += 1
        candidate = min(99, self.completed * 100 // self.total)
        if candidate <= self.persisted:
            return
        changed = await self.repo.db.jobs.update_one(
            {"id": self.job["id"], "status": "RUNNING"},
            {"$set": {"progress": candidate}},
        )
        if changed.modified_count:
            self.persisted = candidate


async def _download(repo: Any, job: dict[str, Any]) -> dict[str, Any]:
    """按小时重组用户下载；用户包只包含日志 tar，不包含索引或清单。"""
    exports = _root(repo) / "exports"
    scratch = exports / ".tmp" / job["id"]
    output = exports / job["id"]
    sources: list[Source] = []
    missing: list[str] = []
    progress: JobProgress | None = job.get("_progress")
    await _reserve_temp(repo, job["id"], _estimated_temp_bytes(job["files"]))
    try:
        await job_thread(scratch.mkdir, parents=True, exist_ok=True)
        scratch_names: set[str] = set()
        for frozen in job["files"]:
            if await _cancelled(repo, job["id"]):
                raise asyncio.CancelledError()
            try:
                file = await _file_doc(repo, frozen)
                local = file.get("nodeId") == repo.settings.node_id
                scratch_name = _unique_scratch_name(
                    _archive_name(file, frozen["id"], use_path=local), scratch_names
                )
                path, temporary = await _archive(repo, frozen, scratch, scratch_name)
                sources.append((frozen, file, path, temporary))
            except Exception:  # noqa: BLE001 - partial exports deliberately record each unavailable source
                _log.exception("export source unavailable fileId=%s", frozen.get("id"))
                missing.append(frozen.get("id", "unknown"))
            finally:
                if progress is not None:
                    await progress.advance()
        if not sources:
            raise FileNotFoundError("no selected archives are available")
        if missing and not job.get("allowPartial", False):
            raise FileNotFoundError("selected archive is unavailable")
        sources = await job_thread(pin_hour_sources, sources, scratch / "pinned")
        if sum(path.stat().st_size for path in {item[2] for item in sources}) > OUTPUT_LIMIT:
            raise ValueError("export output exceeds 20GB limit")
        by_hour: dict[str, list[Source]] = {}
        for source in sources:
            by_hour.setdefault(str(source[0].get("hour") or source[1].get("hour") or "unknown-hour"), []).append(source)
        hourly: list[Path] = []
        for hour, members in sorted(by_hour.items()):
            if await _cancelled(repo, job["id"]):
                raise asyncio.CancelledError()
            reusable = await job_thread(reusable_hour_archive, members)
            if reusable is not None:
                hourly.append(reusable)
                continue
            hourly_path = scratch / hour_export_name(job, hour)
            await job_thread(write_hour_archive, hourly_path, members, max_output_bytes=OUTPUT_LIMIT)
            hourly.append(hourly_path)
        await job_thread(output.mkdir, parents=True, exist_ok=True)
        result_path: Path
        if len(hourly) == 1:
            filename = hourly[0].name
            if hourly[0] in {path for _frozen, _file, path, _temporary in sources}:
                result_path = output / filename
                try:
                    await job_thread(os.link, hourly[0], result_path)
                    size = result_path.stat().st_size
                except OSError:
                    size = await job_thread(copy_limited, hourly[0], result_path, max_output_bytes=OUTPUT_LIMIT)
            else:
                size = await job_thread(copy_limited, hourly[0], output / filename, max_output_bytes=OUTPUT_LIMIT)
                result_path = output / filename
        else:
            task_component = safe_filename_component(str(job.get("taskName") or job.get("taskId") or job["id"]), fallback=job["id"])
            filename = f"{task_component}-hours.zip"
            destination = output / filename
            size = await job_thread(write_zip, destination, hourly, OUTPUT_LIMIT)
            result_path = destination
        if size > OUTPUT_LIMIT:
            raise ValueError("export output exceeds 20GB limit")
        return {"resultPath": str(result_path), "filename": filename, "missing": missing, "bytes": size}
    except BaseException:
        await job_thread(shutil.rmtree, output, True)
        raise
    finally:
        try:
            await job_thread(shutil.rmtree, scratch, True)
        finally:
            await _release_temp(job["id"])


def _scan_archive(path: Path, scanner: StreamSearch, file: dict, cancelled, archive_member: str | None = None):
    """按旁路索引关联接收时间，流式扫描解压后的日志正文。"""
    if path.suffix == ".log":
        with path.open("rb") as raw:
            yield from scanner.scan(LimitedReader(raw, path.stat().st_size), [], file, cancelled)
        return
    # 正文和索引交替读取不能共享 gzip 游标，否则补读索引会反复回扫整个正文。
    with tarfile.open(path, "r:gz") as archive, tarfile.open(path, "r:gz") as index_archive:
        try:
            raw = archive.getmember(archive_member) if archive_member else next((item for item in archive.getmembers() if item.name.endswith(".log")), None)
        except KeyError:
            raw = None
        index = next((item for item in archive.getmembers() if item.name.endswith(".index.jsonl")), None)
        if raw is None or not raw.name.endswith(".log"):
            raise FileNotFoundError(archive_member or path.name)
        entries = index_entries(LimitedReader(index_archive.extractfile(index), index.size), cancelled) if index else []
        stream = archive.extractfile(raw)
        if stream:
            yield from scanner.scan(LimitedReader(stream, raw.size), entries, file, cancelled)


def _search_archive(path: Path, needle: bytes, start: datetime, end: datetime, cancelled, archive_member: str | None = None):
    """单归档扫描入口，保留偏移与文本的调用合同。"""
    for result in _scan_archive(path, StreamSearch(needle, start, end), {}, cancelled, archive_member):
        yield result["offset"], result["text"]


def _search_limited(path: Path, scanner: StreamSearch, file: dict, limit: int, cancelled, archive_member: str | None = None):
    """累计有界结果，匹配器继续保留本作业下一连续文件所需的尾部。"""
    results = []
    for result in _scan_archive(path, scanner, file, cancelled, archive_member):
        results.append(result)
        if len(results) >= limit:
            break
    return results


async def _search(repo: Any, job: dict[str, Any]) -> dict[str, Any]:
    """搜索最多返回一千项，记录截断标识并始终清理本作业临时目录。"""
    needle = job.get("keyword", "").encode()
    if not needle:
        raise ValueError("search keyword is required")
    start = datetime.fromisoformat(job["start"])
    end = datetime.fromisoformat(job["end"])
    scanner = StreamSearch(needle, start, end)
    results, truncated = [], False
    progress: JobProgress | None = job.get("_progress")
    scratch, stopped = _root(repo) / "exports" / ".tmp" / job["id"], threading.Event()
    # 每次只保留一个快照；以实际写入上限预留，不能把正文大小当作含索引压缩包上限。
    await _reserve_temp(repo, job["id"], SEARCH_SNAPSHOT_LIMIT)
    try:
        await job_thread(scratch.mkdir, parents=True, exist_ok=True)
        for frozen in job["files"]:
            if await _cancelled(repo, job["id"]):
                stopped.set()
                raise asyncio.CancelledError()
            file = await _file_doc(repo, frozen)
            path, temporary = await _archive(repo, frozen, scratch, include_index=True, max_output_bytes=SEARCH_SNAPSHOT_LIMIT)
            matches = await job_thread(_search_limited, path, scanner, frozen,
                1000 - len(results), stopped.is_set, file.get("archiveMember"), stop=stopped)
            if temporary:
                await job_thread(path.unlink, missing_ok=True)
            if progress is not None:
                await progress.advance()
            for match in matches:
                results.append(match)
                if len(results) >= 1000:
                    truncated = True
                    return {"results": results, "truncated": truncated}
        return {"results": results, "truncated": truncated}
    finally:
        stopped.set()
        try:
            await job_thread(shutil.rmtree, scratch, True)
        finally:
            await _release_temp(job["id"])


async def run_job(repo: Any, job: dict[str, Any]) -> dict[str, Any]:
    """在节点并发上限内执行冻结作业，条件更新避免覆盖已经取消的状态。"""
    async with _jobs:
        job["_progress"] = JobProgress(repo, job)
        try:
            if await _cancelled(repo, job["id"]):
                return {"status": "CANCELLED"}
            if _expired(job):
                await repo.db.jobs.update_one({"id": job["id"], "status": "RUNNING"}, {"$set": {"status": "EXPIRED"}})
                return {"status": "EXPIRED"}
            result = await (_download(repo, job) if job["kind"] == "DOWNLOAD" else _search(repo, job))
            update = result | {"status": "SUCCEEDED", "progress": 100, "completedAt": datetime.now(UTC)}
            changed = await repo.db.jobs.update_one({"id": job["id"], "status": "RUNNING"}, {"$set": update})
            if not changed.modified_count:
                await job_thread(shutil.rmtree, _root(repo) / "exports" / job["id"], True)
                return {"status": "CANCELLED"}
            await repo.audit(job.get("actor", "system"), "job_succeeded", job["id"])
            return update
        except asyncio.CancelledError:
            await repo.db.jobs.update_one({"id": job["id"], "status": "RUNNING"}, {"$set": {"status": "CANCELLED"}})
            return {"status": "CANCELLED"}
        except Exception as error:  # noqa: BLE001 - job failures are persisted for every operational exception
            _log.exception("job failed id=%s kind=%s", job.get("id"), job.get("kind"))
            update = {"status": "FAILED", "error": type(error).__name__, "completedAt": datetime.now(UTC)}
            await repo.db.jobs.update_one({"id": job["id"], "status": "RUNNING"}, {"$set": update})
            try:
                await repo.audit(job.get("actor", "system"), "job_failed", job["id"])
            except Exception:  # noqa: BLE001 - audit failure must not hide the original job failure
                _log.exception("failed to audit job failure id=%s", job.get("id"))
            return update
        finally:
            job.pop("_progress", None)
