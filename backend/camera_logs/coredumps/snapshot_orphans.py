"""无 catalog 引用的过期快照 claim 回收，严格按 fileId/version/token 删除私有副本。"""

import logging
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from camera_logs.common.database import now
from camera_logs.coredumps.safety import snapshots_root
from camera_logs.logs.job_threads import job_thread

logger = logging.getLogger(__name__)


def _expired(value: datetime, stamp: datetime) -> bool:
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
    return value <= stamp


def _names(claim: dict[str, Any]) -> re.Pattern[str] | None:
    file_id, token, version = claim.get("fileId"), claim.get("id"), claim.get("version")
    if not isinstance(file_id, str) or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", file_id) or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return None
    if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 0):
        return None
    version_part = str(version) if version is not None else r"\d+"
    return re.compile(rf"^(?:\.{re.escape(file_id)}-{version_part}\.{re.escape(token)}\.partial|{re.escape(file_id)}-{version_part}\.{re.escape(token)}\.core)$")


def _delete_owned(root: Path, claim: dict[str, Any]) -> None:
    """逐项扫描受控根，不跟随链接且仅删除精确 claim token 的常规文件。"""
    pattern = _names(claim)
    if pattern is None:
        raise RuntimeError("旧快照 claim 缺少安全文件标识")
    if claim.get("version") is not None:
        file_id, version, token = claim["fileId"], claim["version"], claim["id"]
        paths = [root / f".{file_id}-{version}.{token}.partial", root / f"{file_id}-{version}.{token}.core"]
        for path in paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise RuntimeError("孤儿快照路径不是常规文件")
            path.unlink()
        return
    try:
        entries = os.scandir(root)
    except FileNotFoundError:
        return
    with entries:
        for entry in entries:
            if not pattern.fullmatch(entry.name):
                continue
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise RuntimeError("孤儿快照路径不是常规文件")
            Path(entry.path).unlink()


async def reconcile_orphans(repo: Any, *, timestamp: datetime | None = None) -> int:
    """回收无引用 claim；单个删除失败只记录并保留 RECLAIMING，不能阻塞其它 token。"""
    from camera_logs.coredumps.snapshots import _release

    stamp, root, removed = timestamp or now(), snapshots_root(repo.settings), 0
    cursor = repo.db.coredump_snapshot_claims.find(
        {
            "nodeId": repo.settings.node_id,
            "$or": [
                {"state": "RECLAIMING"},
                {"state": {"$in": ["RESERVED", "PUBLISHED"]}, "expiresAt": {"$lte": stamp}},
            ],
        }
    )
    async for claim in cursor:
        try:
            if claim.get("state") != "RECLAIMING" and (not claim.get("expiresAt") or not _expired(claim["expiresAt"], stamp)):
                continue
            token = claim["id"]
            referenced = await repo.db.coredump_files.find_one({"nodeId": repo.settings.node_id, "$or": [{"freezeToken": token}, {"snapshot.reservationToken": token}]})
            if referenced:
                continue
            claimed = await repo.db.coredump_snapshot_claims.update_one(
                {"id": token, "nodeId": repo.settings.node_id, "state": {"$in": ["RESERVED", "PUBLISHED", "RECLAIMING"]}},
                {"$set": {"state": "RECLAIMING", "reclaimStartedAt": stamp}},
            )
            if claimed.matched_count != 1:
                continue
            await job_thread(_delete_owned, root, claim)
            await _release(repo, token)
            removed += 1
        except Exception:
            logger.exception("孤儿 coredump 快照回收失败 token=%s", claim.get("id"))
    return removed
