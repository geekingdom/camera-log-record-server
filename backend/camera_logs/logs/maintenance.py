"""本节点归档保留期维护及文件目录恢复。

只清理已校验发布、超过保留期且未被导出保护的本地文件；数据库先声明删除
状态，避免新导出和清理竞争。节点重启时扫描已发布归档修复目录登记。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pymongo import ReturnDocument

from camera_logs.common.database import now
from camera_logs.logs.compression import _hash_stream, compress_hour

logger = logging.getLogger(__name__)


def _root(repo: Any) -> Path:
    return Path(repo.settings.log_root).resolve()


def _contained(repo: Any, path: str | Path) -> Path:
    value = Path(path)
    value = value.resolve() if value.is_absolute() else (_root(repo) / value).resolve()
    if _root(repo) not in value.parents:
        raise ValueError("archive path is outside log root")
    return value


def _archive_id(root: Path, path: Path) -> str:
    """根据原始日志相对路径生成稳定目录 ID，与运行时登记规则一致。"""
    relative = str(path.relative_to(root)).removesuffix(".log")
    return hashlib.sha256(relative.encode()).hexdigest()[:32]


def _metadata_path(path: Path) -> Path:
    """共享小时归档的成员清单固定放在同目录旁路文件中。"""
    return Path(str(path) + ".metadata.json")


def _pending_path(path: Path) -> Path:
    """压缩事务清单存在时，归档尚未完成 metadata 发布和原始分卷清理。"""
    return Path(str(path) + ".pending.json")


def _pending_segments(path: Path) -> list[dict[str, Any]] | None:
    """读取待提交清单；格式损坏交由压缩器拒绝，维护不把它误作已完成归档。"""
    pending = _pending_path(path)
    if not pending.is_file() or pending.stat().st_size > 1024 * 1024:
        return None
    value = json.loads(pending.read_text(encoding="utf-8"))
    segments = value.get("segments") if isinstance(value, dict) else None
    return segments if isinstance(value, dict) and value.get("formatVersion") == 1 and isinstance(segments, list) and segments else None


async def _pending_is_active(repo: Any, segments: list[dict[str, Any]]) -> bool:
    """当前节点仍使用该任务和会话时，让其自身压缩流程完成事务，避免并行改写。"""
    for segment in segments:
        current = await repo.db.tasks.find_one({
            "id": segment.get("taskId"), "runId": segment.get("runId"), "sessionId": segment.get("sessionId"),
            "nodeId": repo.settings.node_id, "status": {"$in": ["CONNECTING", "COLLECTING", "RECONNECTING", "PAUSING"]},
        })
        if current is not None:
            return True
    return False


def _member_path(archive: Path, name: Any, suffix: str) -> Path:
    """限制外部成员名称为当前小时目录的普通文件，阻止旁路元数据逃逸目录。"""
    if not isinstance(name, str) or not name.endswith(suffix) or Path(name).name != name:
        raise ValueError("invalid archive sidecar member name")
    return archive.parent / name


def _legacy_members(path: Path) -> list[dict[str, Any]]:
    """兼容旧单分卷 manifest，避免已发布历史归档在每轮维护中重复报错。"""
    with tarfile.open(path, "r:gz") as archive:
        manifest_file = archive.getmember("manifest.json")
        stream = archive.extractfile(manifest_file)
        if not manifest_file.isfile() or manifest_file.size > 1024 * 1024 or stream is None:
            raise ValueError("invalid legacy archive manifest")
        manifest = json.loads(stream.read())
        log = next((item for item in archive.getmembers() if item.isfile() and item.name.endswith(".log")), None)
        if log is None or not isinstance(manifest, dict):
            raise ValueError("invalid legacy archive log")
        source = archive.extractfile(log)
        if source is None or _hash_stream(source) != (manifest["sha256"], manifest["rawSize"]):
            raise ValueError("legacy archive checksum or size mismatch")
        index = next((item for item in archive.getmembers() if item.isfile() and item.name.endswith(".index.jsonl")), None)
        if index is not None:
            source = archive.extractfile(index)
            if source is None or _hash_stream(source) != (manifest["indexSha256"], manifest["indexBytes"]):
                raise ValueError("legacy archive index checksum or size mismatch")
            source.seek(0)
            first = source.readline()
            if first:
                manifest["firstReceivedAt"] = json.loads(first).get("receivedAt")
        return [manifest | {"logName": log.name, "_legacy": True}]


def _members(path: Path) -> list[dict[str, Any]]:
    """校验共享小时 tar 正文及元数据引用的外部索引，返回成员清单。"""
    metadata_path = _metadata_path(path)
    if not metadata_path.is_file():
        return _legacy_members(path)
    if metadata_path.stat().st_size > 1024 * 1024:
        raise ValueError("archive metadata is too large")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    members = metadata.get("members") if isinstance(metadata, dict) else None
    if not isinstance(metadata, dict) or metadata.get("formatVersion") != 2 or not isinstance(members, list) or not members:
        raise ValueError("invalid archive metadata")
    required = {"taskId", "runId", "sessionId", "hourStart", "rawSize", "sha256", "firstSequence", "lastSequence",
                "logName", "indexName", "indexSha256", "indexBytes"}
    names: set[str] = set()
    with tarfile.open(path, "r:gz") as archive:
        archive_members = {item.name: item for item in archive.getmembers() if item.isfile()}
        if len(archive_members) != len(members) or any(not name.endswith(".log") for name in archive_members):
            raise ValueError("archive contains non-log or duplicate members")
        for member in members:
            if not isinstance(member, dict) or not required <= member.keys():
                raise ValueError("archive metadata member is incomplete")
            log_path = _member_path(path, member["logName"], ".log")
            index_path = _member_path(path, member["indexName"], ".index.jsonl")
            if log_path.name in names or not isinstance(member["rawSize"], int) or not isinstance(member["indexBytes"], int):
                raise ValueError("archive metadata member is invalid")
            names.add(log_path.name)
            source = archive.extractfile(archive_members.get(log_path.name))
            if source is None or _hash_stream(source) != (member["sha256"], member["rawSize"]):
                raise ValueError("archive log checksum or size mismatch")
            with index_path.open("rb") as index:
                if _hash_stream(index) != (member["indexSha256"], member["indexBytes"]):
                    raise ValueError("archive index checksum or size mismatch")
                index.seek(0)
                first = index.readline()
                if first:
                    member["firstReceivedAt"] = json.loads(first).get("receivedAt")
    return members


def _sidecar_member_names(path: Path) -> set[str] | None:
    """仅读取小型元数据以判断已登记归档是否可跳过逐字节恢复校验。"""
    metadata_path = _metadata_path(path)
    if not metadata_path.is_file() or metadata_path.stat().st_size > 1024 * 1024:
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    members = metadata.get("members") if isinstance(metadata, dict) else None
    if not isinstance(metadata, dict) or metadata.get("formatVersion") != 2 or not isinstance(members, list) or not members:
        return None
    names = {item.get("logName") for item in members if isinstance(item, dict)}
    return names if len(names) == len(members) and all(isinstance(name, str) for name in names) else None


async def _ready_group_matches(repo: Any, path: Path) -> bool:
    """大小和成员数均未变化的完整 READY 组无需每次维护重新读取全部 tar 正文。"""
    names = await asyncio.to_thread(_sidecar_member_names, path)
    if names is None:
        return False
    records = [record async for record in repo.db.files.find({"path": str(path), "status": "READY"})]
    return (len(records) == len(names) and {record.get("archiveMember") for record in records} == names
            and all(record.get("archiveBytes") == path.stat().st_size for record in records))


async def recover_orphan_archives(repo: Any) -> int:
    """节点重启后根据已发布归档清单补回目录，不改写未封存的日志正文。"""
    root = _root(repo)
    registered = 0
    for path in root.rglob("*.tar.gz"):
        if "exports" in path.parts:
            continue
        try:
            pending = await asyncio.to_thread(_pending_segments, path)
            if pending is not None:
                if await _pending_is_active(repo, pending):
                    continue
                await compress_hour([], path)
            if await _ready_group_matches(repo, path):
                continue
            members = await asyncio.to_thread(_members, path)
            group_id = hashlib.sha256(str(path.relative_to(root)).encode()).hexdigest()
            for member in members:
                log_path = _member_path(path, member["logName"], ".log")
                index_path = None if member.get("_legacy") else _member_path(path, member["indexName"], ".index.jsonl")
                identifier = _archive_id(root, log_path)
                existing = await repo.db.files.find_one({"id": identifier})
                if existing and existing.get("status") != "OPEN":
                    continue
                identity = {key: member[key] for key in ("taskId", "runId", "sessionId")}
                identity["nodeId"] = repo.settings.node_id
                if existing and any(existing.get(key) != value for key, value in identity.items()):
                    raise ValueError("archive identity differs from open catalog record")
                document = {
                    "id": identifier, **identity,
                    "hour": datetime.fromisoformat(member["hourStart"]).astimezone(UTC).isoformat(),
                    "path": str(path), "archiveName": path.name, "archiveMember": log_path.name,
                    "archiveGroupId": group_id, "rawFileName": log_path.name,
                    "bytes": member["rawSize"], "archiveBytes": path.stat().st_size, "sha256": member["sha256"],
                    "firstSequence": member["firstSequence"], "lastSequence": member["lastSequence"],
                    "segmentNumber": int(match.group(1)) if not member.get("_legacy") and (
                        match := re.search(r"-part-(\d+)\.log$", log_path.name)) else 0,
                    "status": "READY", "updatedAt": now(),
                }
                if index_path is not None:
                    document["indexPath"] = str(index_path)
                if member.get("firstReceivedAt"):
                    document["firstReceivedAt"] = member["firstReceivedAt"]
                if existing:
                    result = await repo.db.files.update_one(
                        {"id": identifier, "status": "OPEN", **identity}, {"$set": document})
                    registered += result.modified_count
                else:
                    result = await repo.db.files.update_one(
                        {"id": identifier}, {"$setOnInsert": {**document, "createdAt": now()}}, upsert=True)
                    registered += int(result.upserted_id is not None)
        except Exception:
            logger.exception("归档恢复失败 path=%s", path)
    return registered


async def apply_retention(repo: Any, *, development_file_ids: frozenset[str] | None = None) -> dict[str, int]:
    """领取整组归档后删除；开发入口仅传入已通过报告/内容校验的有限 ID 集合。

    开发清理只绕过自然保留天数，仍受下载保护、作业、节点和成员组约束，且任务
    必须已随资源删除而永久停用。正常维护不传集合，继续使用平台保留天数。
    """
    cutoff = (now() if development_file_ids is not None else
              now() - timedelta(days=await get_retention_days(repo)) - timedelta(hours=1))
    removed = skipped = failures = 0
    query = {
        "nodeId": repo.settings.node_id, "$or": [
            {"status": "READY", "hour": {"$lt": cutoff.isoformat()}},
            {"status": "DELETING"},
        ],
    }
    if development_file_ids is not None:
        query["id"] = {"$in": sorted(development_file_ids)}
    cursor = repo.db.files.find(query)
    async for document in cursor:
        # 一个共享小时 tar 可被多个 files 成员引用。先读取完整成员组，任一
        # 受保护、未过期、非本节点或未进入可删除状态都阻止物理删除。
        if _pending_path(Path(document["path"])).exists():
            skipped += 1
            continue
        group = [item async for item in repo.db.files.find({"path": document["path"]})]
        eligible = bool(group)
        for member in group:
            if development_file_ids is not None:
                task = await repo.db.tasks.find_one({"id": member.get("taskId"),
                    "resourceDeleted": True, "status": "STOPPED", "desiredState": "STOPPED", "nodeId": None})
                if member["id"] not in development_file_ids or task is None:
                    eligible = False
                    break
            if member.get("nodeId") != repo.settings.node_id or member.get("status") not in {"READY", "DELETING"}:
                eligible = False
                break
            if member.get("status") == "READY":
                if member.get("hour", cutoff.isoformat()) >= cutoff.isoformat():
                    eligible = False
                    break
                retain_until = member.get("retainUntil")
                if retain_until is not None:
                    if not isinstance(retain_until, datetime):
                        eligible = False
                        break
                    retain_until = retain_until.replace(tzinfo=UTC) if retain_until.tzinfo is None else retain_until
                    if retain_until > now():
                        eligible = False
                        break
                active = await repo.db.jobs.find_one({
                    "status": {"$in": ["QUEUED", "RUNNING"]}, "files.id": member["id"]
                })
                if active:
                    eligible = False
                    break
        if not eligible:
            skipped += 1
            continue
        for member in group:
            if member.get("status") != "READY":
                continue
            claimed = await repo.db.files.find_one_and_update(
                {"id": member["id"], "status": "READY", "$or": [
                    {"retainUntil": None}, {"retainUntil": {"$lte": now()}},
                ]},
                {"$set": {"status": "DELETING", "retentionClaimedAt": now()}},
                return_document=ReturnDocument.AFTER,
            )
            if not claimed:
                eligible = False
                break
        if not eligible:
            skipped += 1
            continue
        claimed_group = [item async for item in repo.db.files.find({"path": document["path"]})]
        if not claimed_group or any(item.get("status") != "DELETING" for item in claimed_group):
            skipped += 1
            continue
        try:
            path = _contained(repo, document["path"])
            if path.suffixes[-2:] != [".tar", ".gz"]:
                raise ValueError("retention only deletes published archives")
            await asyncio.to_thread(path.unlink, missing_ok=True)
            sidecars = {_metadata_path(path)}
            for member in claimed_group:
                if member.get("indexPath"):
                    sidecars.add(_contained(repo, member["indexPath"]))
            await asyncio.gather(*(asyncio.to_thread(sidecar.unlink, missing_ok=True) for sidecar in sidecars))
            deleted = await repo.db.files.delete_many({"path": str(path), "status": "DELETING"})
            removed += deleted.deleted_count
        except Exception:
            failures += 1
            logger.exception("归档保留清理失败 path=%s", document.get("path"))
            await repo.db.files.update_many(
                {"path": document["path"], "status": "DELETING"},
                {"$set": {"retentionErrorAt": now()}},
            )
    return {"removed": removed, "skipped": skipped, "failures": failures}


async def get_retention_days(repo: Any) -> int:
    """每轮维护读取数据库保留期；缺省或损坏记录安全回退到进程默认值。"""
    fallback = int(getattr(repo.settings, "retention_days", 7))
    collection = getattr(repo.db, "platform_settings", None)
    if collection is None:
        return fallback
    document = await collection.find_one({"id": "platform"})
    value = document.get("retentionDays") if document else fallback
    if isinstance(value, int) and 1 <= value <= 3650:
        return value
    logger.warning("平台保留期配置无效，使用默认值 retentionDays=%s", fallback)
    return fallback


async def cleanup_exports(repo: Any) -> int:
    """清理已到期的本节点终态下载目录，不以目录 mtime 推断是否仍在执行。

    目录名必须精确对应作业 ID；缺少、损坏、远端、取消或活跃作业都保留。
    物理路径只允许是 exports 目录的直系真实子目录，避免清理软链接或根外路径。
    """
    configured_root = Path(repo.settings.log_root)
    if configured_root.is_symlink():
        logger.warning("拒绝清理软链接日志根 path=%s", configured_root)
        return 0
    exports = configured_root / "exports"
    removed = 0
    if exports.is_symlink() or not exports.is_dir():
        if exports.is_symlink():
            logger.warning("拒绝清理软链接导出根 path=%s", exports)
        return removed
    exports_root = exports.resolve()
    for path in exports.iterdir():
        try:
            if path.name == ".tmp" or path.is_symlink() or not path.is_dir():
                continue
            resolved = path.resolve()
            if resolved.parent != exports_root:
                logger.warning("拒绝清理非直系导出目录 path=%s", path)
                continue
            job = await repo.db.jobs.find_one({"id": path.name})
            if not job or job.get("nodeId") != repo.settings.node_id or job.get("kind") != "DOWNLOAD":
                continue
            if job.get("status") not in {"SUCCEEDED", "FAILED", "EXPIRED"}:
                continue
            expires_at, completed_at = job.get("expiresAt"), job.get("completedAt")
            if not isinstance(expires_at, datetime) or not isinstance(completed_at, datetime):
                continue
            expires_at = expires_at.replace(tzinfo=UTC) if expires_at.tzinfo is None else expires_at
            if expires_at > now():
                continue
            await asyncio.to_thread(shutil.rmtree, path)
            removed += 1
        except Exception:
            logger.exception("导出产物清理失败 path=%s", path)
    return removed


async def maintain(repo: Any) -> dict[str, int]:
    recovered = await recover_orphan_archives(repo)
    retention = await apply_retention(repo)
    exports = await cleanup_exports(repo)
    return {"recovered": recovered, "exportsRemoved": exports, **retention}
