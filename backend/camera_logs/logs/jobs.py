"""节点侧的受限导出和字面关键词搜索作业。

文件只在鉴权后的节点内解析，跨节点通过内部接口流式取回；当前小时按创建
作业时冻结的水位截取。并发数、读带宽及产物大小受限，失败和取消持久化回报。
"""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import shutil
import tarfile
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from camera_logs.logs.archive_access import LimitedReader, copy_limited, read_limiter, snapshot
from camera_logs.logs.naming import safe_filename_component

OUTPUT_LIMIT = 20_000_000_000
TEMP_LIMIT = 100_000_000_000
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


async def _remote_archive(repo: Any, file: dict[str, Any], frozen: dict[str, Any], target: Path) -> Path:
    """跨节点取回冻结快照，并限制读取速率和临时文件大小。"""
    node = await repo.get("nodes", file["nodeId"])
    params = {"bytes": frozen.get("bytes", file.get("bytes", 0))} if frozen.get("status") != "READY" else None
    written = 0
    async with httpx.AsyncClient(timeout=120) as client, client.stream("GET", node["url"] + f"/internal/archive/{file['id']}", params=params, headers={"Authorization": "Bearer " + repo.settings.internal_token}) as response:
        response.raise_for_status()
        with target.open("wb") as output:
            async for chunk in response.aiter_bytes(1024 * 1024):
                written += len(chunk)
                if written > TEMP_LIMIT:
                    raise ValueError("remote archive exceeds temporary storage limit")
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
    async with _temp_reservation_lock:
        _temp_reservations.pop(job_id, None)


async def _archive(
    repo: Any,
    frozen: dict[str, Any],
    scratch: Path,
    scratch_name: str | None = None,
) -> tuple[Path, bool]:
    """查找已封存归档或建立固定水位快照，返回路径及是否为临时文件。"""
    file = await _file_doc(repo, frozen)
    local = file.get("nodeId") == repo.settings.node_id
    target = scratch / (scratch_name or _archive_name(file, str(file["id"]), use_path=local))
    if not local:
        await _remote_archive(repo, file, frozen, target)
        return target, True
    path = _file_path(repo, file)
    if frozen.get("status") == "READY" and path.suffixes[-2:] == [".tar", ".gz"]:
        return path, False
    index = _contained(repo, file["indexPath"]) if file.get("indexPath") else None
    await asyncio.to_thread(snapshot, path, target, int(frozen.get("bytes", file.get("bytes", 0))), index, file["id"])
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


async def _download(repo: Any, job: dict[str, Any]) -> dict[str, Any]:
    """组装选中片段；缺失必须显式报告，多片段使用 ZIP STORE 避免再次压缩。"""
    exports = _root(repo) / "exports"
    scratch = exports / ".tmp" / job["id"]
    output = exports / job["id"]
    sources: list[tuple[dict[str, Any], Path, bool]] = []
    missing: list[str] = []
    await _reserve_temp(repo, job["id"], _estimated_temp_bytes(job["files"]))
    try:
        await asyncio.to_thread(scratch.mkdir, parents=True, exist_ok=True)
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
                sources.append((frozen, path, temporary))
            except Exception:  # noqa: BLE001 - partial exports deliberately record each unavailable source
                _log.exception("export source unavailable fileId=%s", frozen.get("id"))
                missing.append(frozen.get("id", "unknown"))
        if not sources:
            raise FileNotFoundError("no selected archives are available")
        if missing and not job.get("allowPartial", False):
            raise FileNotFoundError("selected archive is unavailable")
        if sum(path.stat().st_size for _, path, _ in sources) > OUTPUT_LIMIT:
            raise ValueError("export output exceeds 20GB limit")
        if len(sources) == 1 and not missing:
            filename = sources[0][1].name
            size = await asyncio.to_thread(copy_limited, sources[0][1], output / filename)
        else:
            filename = f"{job['id']}.zip"
            destination = output / filename
            def make_zip() -> int:
                manifest = {"jobId": job["id"], "files": [item[0]["id"] for item in sources], "missing": missing}
                with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as bundle:
                    used_names: set[str] = set()
                    for _frozen, path, _ in sources:
                        entry_name = path.name
                        suffix = "".join(Path(entry_name).suffixes)
                        stem = entry_name.removesuffix(suffix)
                        while entry_name in used_names:
                            entry_name = f"{stem}-{len(used_names) + 1}{suffix}"
                        used_names.add(entry_name)
                        with path.open("rb") as source, bundle.open(entry_name, "w") as entry:
                            while data := source.read(1024 * 1024):
                                read_limiter.consume(len(data))
                                entry.write(data)
                    bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True))
                return destination.stat().st_size
            await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
            size = await asyncio.to_thread(make_zip)
        if size > OUTPUT_LIMIT:
            raise ValueError("export output exceeds 20GB limit")
        return {"resultPath": str(output / filename), "filename": filename, "missing": missing, "bytes": size}
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, output, True)
        raise
    finally:
        await asyncio.to_thread(shutil.rmtree, scratch, True)
        await _release_temp(job["id"])


def _blocks(stream: Any, needle: bytes, index: list[dict[str, Any]], start: datetime, end: datetime, cancelled):
    """分块匹配关键词，用少量尾部重叠处理跨块匹配并避免重复返回。"""
    carry, offset = b"", 0
    offsets = [item["offset"] for item in index]
    while chunk := stream.read(256 * 1024):
        if cancelled():
            raise InterruptedError("job cancelled")
        data = carry + chunk
        begin = 0
        while (found := data.find(needle, begin)) >= 0:
            absolute = offset - len(carry) + found
            # 重叠前缀已在前一块处理，只有跨越边界的匹配才能再次进入结果。
            if absolute >= offset or found + len(needle) > len(carry):
                slot = bisect.bisect_right(offsets, absolute) - 1
                entry = index[slot] if slot >= 0 and absolute < index[slot]["offset"] + index[slot]["length"] else None
                stamp = datetime.fromisoformat(entry["receivedAt"]) if entry else None
                if stamp is None or start <= stamp <= end:
                    yield absolute, data[max(0, found - 120):found + len(needle) + 120].decode("utf-8", "replace")
            begin = found + max(1, len(needle))
        carry = data[-max(1, len(needle) - 1):]
        offset += len(chunk)


def _search_archive(path: Path, needle: bytes, start: datetime, end: datetime, cancelled):
    """按旁路索引关联接收时间，流式扫描解压后的日志正文。"""
    if path.suffix == ".log":
        with path.open("rb") as raw:
            yield from _blocks(LimitedReader(raw, path.stat().st_size), needle, [], start, end, cancelled)
        return
    with tarfile.open(path, "r:gz") as archive:
        raw = next((item for item in archive.getmembers() if item.name.endswith(".log")), None)
        index = next((item for item in archive.getmembers() if item.name.endswith(".index.jsonl")), None)
        entries = [json.loads(line) for line in LimitedReader(archive.extractfile(index), index.size).read().splitlines()] if index else []
        stream = archive.extractfile(raw)
        if stream:
            yield from _blocks(LimitedReader(stream, raw.size), needle, entries, start, end, cancelled)


def _search_limited(path: Path, needle: bytes, start: datetime, end: datetime, limit: int, cancelled):
    results = []
    for offset, text in _search_archive(path, needle, start, end, cancelled):
        results.append((offset, text))
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
    results, truncated = [], False
    scratch, stopped = _root(repo) / "exports" / ".tmp" / job["id"], threading.Event()
    await asyncio.to_thread(scratch.mkdir, parents=True, exist_ok=True)
    try:
        for frozen in job["files"]:
            if await _cancelled(repo, job["id"]):
                stopped.set()
                raise asyncio.CancelledError()
            path, _temporary = await _archive(repo, frozen, scratch)
            matches = await asyncio.to_thread(_search_limited, path, needle, start, end, 1000 - len(results), stopped.is_set)
            for offset, text in matches:
                results.append({"fileId": frozen["id"], "offset": offset, "text": text})
                if len(results) >= 1000:
                    truncated = True
                    return {"results": results, "truncated": truncated}
        return {"results": results, "truncated": truncated}
    finally:
        stopped.set()
        await asyncio.to_thread(shutil.rmtree, scratch, True)


async def run_job(repo: Any, job: dict[str, Any]) -> dict[str, Any]:
    """在节点并发上限内执行冻结作业，条件更新避免覆盖已经取消的状态。"""
    async with _jobs:
        try:
            if await _cancelled(repo, job["id"]):
                return {"status": "CANCELLED"}
            if _expired(job):
                await repo.db.jobs.update_one({"id": job["id"], "status": "RUNNING"}, {"$set": {"status": "EXPIRED"}})
                return {"status": "EXPIRED"}
            result = await (_download(repo, job) if job["kind"] == "DOWNLOAD" else _search(repo, job))
            update = result | {"status": "SUCCEEDED", "completedAt": datetime.now(UTC)}
            changed = await repo.db.jobs.update_one({"id": job["id"], "status": "RUNNING"}, {"$set": update})
            if not changed.modified_count:
                await asyncio.to_thread(shutil.rmtree, _root(repo) / "exports" / job["id"], True)
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
