"""冻结副本的退休、删除和崩溃恢复；所有路径均保留 catalog 事实直到物理收尾。"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from camera_logs.common.database import now


def _valid_readers(document: dict[str, Any], stamp: datetime) -> bool:
    """兼容 Mongo 驱动读回的朴素 UTC reader lease。"""
    normalized = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
    for item in document.get("snapshotReaders", []):
        expiry = item.get("expiresAt")
        if expiry and (expiry.replace(tzinfo=UTC) if expiry.tzinfo is None else expiry.astimezone(UTC)) > normalized:
            return True
    return False


async def release_snapshot(repo: Any, document: dict[str, Any]) -> bool:
    """将 FROZEN 标为 RETIRING；实际删除由 reconcile 在无有效 reader 后完成。"""
    snapshot = document.get("snapshot") or {}
    token = snapshot.get("reservationToken")
    if document.get("nodeId") != repo.settings.node_id or not token:
        return False
    changed = await repo.db.coredump_files.update_one(
        {"id": document["id"], "status": "FROZEN", "snapshot.reservationToken": token},
        {"$set": {"status": "RETIRING", "retireToken": token, "retireStartedAt": now()}},
    )
    if changed.matched_count != 1:
        return False
    current = await repo.db.coredump_files.find_one({"id": document["id"], "retireToken": token})
    if current:
        await _finish(repo, current, now())
    return True


async def _finish(repo: Any, document: dict[str, Any], stamp: datetime) -> bool:
    """无有效读者时 CAS 到 DELETING，unlink 后才释放配额并删除 catalog 引用。"""
    from camera_logs.coredumps.snapshots import _release, snapshots_root

    snapshot, token = document.get("snapshot") or {}, document.get("retireToken")
    if not token or snapshot.get("reservationToken") != token:
        return False
    if document.get("status") == "RETIRING":
        if _valid_readers(document, stamp):
            return False
        claimed = await repo.db.coredump_files.update_one(
            {"id": document["id"], "status": "RETIRING", "retireToken": token,
             "snapshotReaders": {"$not": {"$elemMatch": {"expiresAt": {"$gt": stamp}}}}},
            {"$set": {"status": "DELETING", "deleteStartedAt": stamp}},
        )
        if claimed.matched_count != 1:
            return False
    elif document.get("status") != "DELETING":
        return False
    path = Path(snapshot.get("path", ""))
    root = snapshots_root(repo.settings)
    if path.parent != root or not path.name.endswith(f".{token}.core"):
        raise RuntimeError("coredump 快照路径不受控")
    path.unlink(missing_ok=True)
    await _release(repo, token)
    cleared = await repo.db.coredump_files.update_one(
        {"id": document["id"], "status": "DELETING", "retireToken": token},
        {"$set": {"status": "RECEIVING", "updatedAt": now()},
         "$unset": {"snapshot": "", "snapshotReaders": "", "retireToken": "", "retireStartedAt": "", "deleteStartedAt": ""}},
    )
    return cleared.matched_count == 1


async def reconcile_snapshots(repo: Any, *, timestamp: datetime | None = None) -> int:
    """推进退休副本；FREEZING 恢复仍由 snapshots 原有逻辑处理。"""
    stamp, completed = timestamp or now(), 0
    async for document in repo.db.coredump_files.find({"nodeId": repo.settings.node_id, "status": {"$in": ["RETIRING", "DELETING"]}}):
        await repo.db.coredump_files.update_one({"id": document["id"]}, {"$pull": {"snapshotReaders": {"expiresAt": {"$lte": stamp}}}})
        current = await repo.db.coredump_files.find_one({"id": document["id"]})
        if current and await _finish(repo, current, stamp):
            completed += 1
    return completed
