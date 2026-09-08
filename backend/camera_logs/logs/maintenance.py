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
    with tarfile.open(path, "r:gz") as archive:
        member = archive.getmember("manifest.json")
        stream = archive.extractfile(member)
        if stream is None:
            raise ValueError("archive manifest is unreadable")
        return json.loads(stream.read())


async def recover_orphan_archives(repo: Any) -> int:
    """节点重启后根据已发布归档清单补回目录，不改写未封存的日志正文。"""
    root = _root(repo)
    registered = 0
    for path in root.rglob("*.tar.gz"):
        if "exports" in path.parts:
            continue
        identifier = _archive_id(root, path)
        if await repo.db.files.find_one({"id": identifier}):
            continue
        try:
            manifest = await asyncio.to_thread(_manifest, path)
            hour = datetime.fromisoformat(manifest["hourStart"]).astimezone(UTC).isoformat()
            await repo.db.files.update_one(
                {"id": identifier},
                {"$set": {
                    "id": identifier, "taskId": manifest["taskId"], "runId": manifest["runId"],
                    "sessionId": manifest["sessionId"], "nodeId": repo.settings.node_id,
                    "hour": hour, "path": str(path), "archiveName": path.name, "bytes": manifest["rawSize"],
                    "sha256": manifest["sha256"], "firstSequence": manifest.get("firstSequence"),
                    "lastSequence": manifest.get("lastSequence"), "status": "READY", "updatedAt": now(),
                }, "$setOnInsert": {"createdAt": now()}}, upsert=True,
            )
            registered += 1
        except Exception:
            logger.exception("归档恢复失败 path=%s", path)
    return registered


async def apply_retention(repo: Any) -> dict[str, int]:
    """Delete only expired, ready archives with no queued/running job reference."""
    cutoff = now() - timedelta(days=repo.settings.retention_days) - timedelta(hours=1)
    removed = skipped = failures = 0
    cursor = repo.db.files.find({
        "status": "READY", "nodeId": repo.settings.node_id, "hour": {"$lt": cutoff.isoformat()}
    })
    async for document in cursor:
        active = await repo.db.jobs.find_one({"status": {"$in": ["QUEUED", "RUNNING"]}, "files.id": document["id"]})
        if active:
            skipped += 1
            continue
        claimed = await repo.db.files.find_one_and_update(
            {"id": document["id"], "status": "READY", "retainUntil": {"$lte": now()}},
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
                {"$set": {"status": "READY", "retentionErrorAt": now()}},
            )
    return {"removed": removed, "skipped": skipped, "failures": failures}


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
