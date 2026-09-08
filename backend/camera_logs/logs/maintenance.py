"""本节点归档保留期维护及文件目录恢复。

只清理已校验发布、超过保留期且未被导出保护的本地文件；数据库先声明删除
状态，避免新导出和清理竞争。节点重启时扫描已发布归档修复目录登记。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pymongo import ReturnDocument

from camera_logs.common.database import now
from camera_logs.logs.compression import _hash_stream

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
    relative = str(path.relative_to(root)).removesuffix(".tar.gz")
    return hashlib.sha256(relative.encode()).hexdigest()[:32]


def _manifest(path: Path) -> dict[str, Any]:
    """恢复前重新校验正文和索引；只读取归档成员，不解包到文件系统。"""
    with tarfile.open(path, "r:gz") as archive:
        member = archive.getmember("manifest.json")
        if not member.isfile() or member.size > 1024 * 1024:
            raise ValueError("invalid archive manifest")
        stream = archive.extractfile(member)
        if stream is None:
            raise ValueError("archive manifest is unreadable")
        manifest = json.loads(stream.read())
        checks = [(".log", manifest["sha256"], manifest["rawSize"])]
        if "indexSha256" in manifest or "indexBytes" in manifest:
            checks.append((".index.jsonl", manifest["indexSha256"], manifest["indexBytes"]))
        for suffix, digest, size in checks:
            members = [item for item in archive.getmembers() if item.name.endswith(suffix)]
            if len(members) != 1 or not members[0].isfile():
                raise ValueError("invalid archive data members")
            source = archive.extractfile(members[0])
            if source is None or _hash_stream(source) != (digest, size):
                raise ValueError("archive checksum or size mismatch")
        return manifest


async def recover_orphan_archives(repo: Any) -> int:
    """节点重启后根据已发布归档清单补回目录，不改写未封存的日志正文。"""
    root = _root(repo)
    registered = 0
    for path in root.rglob("*.tar.gz"):
        if "exports" in path.parts:
            continue
        identifier = _archive_id(root, path)
        existing = await repo.db.files.find_one({"id": identifier})
        if existing and existing.get("status") != "OPEN":
            continue
        try:
            manifest = await asyncio.to_thread(_manifest, path)
            identity = {key: manifest[key] for key in ("taskId", "runId", "sessionId")}
            identity["nodeId"] = repo.settings.node_id
            if existing and any(existing.get(key) != value for key, value in identity.items()):
                raise ValueError("archive identity differs from open catalog record")
            hour = datetime.fromisoformat(manifest["hourStart"]).astimezone(UTC).isoformat()
            document = {
                "id": identifier, **identity,
                "hour": hour, "path": str(path), "archiveName": path.name, "bytes": manifest["rawSize"],
                "archiveBytes": path.stat().st_size,
                "sha256": manifest["sha256"], "firstSequence": manifest.get("firstSequence"),
                "lastSequence": manifest.get("lastSequence"), "status": "READY", "updatedAt": now(),
            }
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


async def apply_retention(repo: Any) -> dict[str, int]:
    """Delete only expired, ready archives with no queued/running job reference."""
    cutoff = now() - timedelta(days=await get_retention_days(repo)) - timedelta(hours=1)
    removed = skipped = failures = 0
    cursor = repo.db.files.find({
        "nodeId": repo.settings.node_id, "$or": [
            {"status": "READY", "hour": {"$lt": cutoff.isoformat()}},
            {"status": "DELETING"},
        ],
    })
    async for document in cursor:
        # DELETING 已拒绝新的下载保护，可幂等续做崩溃前尚未完成的物理删除。
        claimed = document if document["status"] == "DELETING" else None
        if claimed is None:
            active = await repo.db.jobs.find_one({"status": {"$in": ["QUEUED", "RUNNING"]}, "files.id": document["id"]})
            if active:
                skipped += 1
                continue
            claimed = await repo.db.files.find_one_and_update(
                {"id": document["id"], "status": "READY", "$or": [
                    {"retainUntil": None}, {"retainUntil": {"$lte": now()}},
                ]},
                {"$set": {"status": "DELETING", "retentionClaimedAt": now()}},
                return_document=ReturnDocument.AFTER,
            )
        if not claimed:
            continue
        try:
            path = _contained(repo, claimed["path"])
            if path.suffixes[-2:] != [".tar", ".gz"]:
                raise ValueError("retention only deletes published archives")
            await asyncio.to_thread(path.unlink, missing_ok=True)
            await repo.db.files.delete_one({"id": claimed["id"], "status": "DELETING"})
            removed += 1
        except Exception:
            failures += 1
            logger.exception("归档保留清理失败 fileId=%s", claimed.get("id"))
            await repo.db.files.update_one(
                {"id": claimed["id"], "status": "DELETING"},
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
    exports = _root(repo) / "exports"
    cutoff = datetime.now(UTC).timestamp() - 24 * 60 * 60
    removed = 0
    if not exports.is_dir():
        return removed
    for path in exports.iterdir():
        if path.name == ".tmp" or path.stat().st_mtime >= cutoff:
            continue
        try:
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
