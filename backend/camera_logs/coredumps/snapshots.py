"""NFS coredump 的不可变副本、带租约配额和可恢复清理。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import stat
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.coredumps.safety import open_source, regular_unlinked_file, snapshots_root
from camera_logs.logs.archive_access import read_limiter

FREEZE_LEASE_SECONDS = 300
RESERVATION_LEASE_SECONDS = 900
logger = logging.getLogger(__name__)


def _expired(value: datetime, stamp: datetime) -> bool:
    """兼容旧 Mongo 驱动读回的无时区 UTC 时间。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return value <= stamp


def _active_after(value: datetime, stamp: datetime) -> bool:
    """与 _expired 对称地比较冻结租约，保留无时区旧记录兼容。"""
    return not _expired(value, stamp)


def _same(left: os.stat_result, right: os.stat_result) -> bool:
    """复制前后必须是同一 inode、长度和时间版本，变化即丢弃副本。"""
    return (left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns, left.st_ctime_ns) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _copy_stable(descriptor: int, target: Path, expected: dict[str, int], limit: int) -> tuple[int, str]:
    """流式复制；本函数接管并且只关闭一次源描述符。"""
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("coredump 必须是非硬链接普通文件")
        if before.st_size > limit:
            raise OverflowError("单个 coredump 超过节点快照上限")
        if any(
            value != expected.get(key)
            for value, key in (
                (before.st_dev, "device"),
                (before.st_ino, "inode"),
                (before.st_size, "size"),
                (before.st_mtime_ns, "mtimeNs"),
                (before.st_ctime_ns, "ctimeNs"),
            )
        ):
            raise RuntimeError("源文件版本已变化")
        digest, written = hashlib.sha256(), 0
        with (
            os.fdopen(descriptor, "rb", buffering=0, closefd=False) as reader,
            target.open("xb", buffering=0) as output,
        ):
            while chunk := reader.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise OverflowError("单个 coredump 超过节点快照上限")
                read_limiter.consume(len(chunk))
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    count = output.write(view)
                    if count is None or count <= 0:
                        raise OSError("coredump 快照写入失败")
                    view = view[count:]
            output.flush()
            os.fsync(output.fileno())
        after, copied = os.fstat(descriptor), regular_unlinked_file(target)
        if not _same(before, after) or copied.st_size != before.st_size:
            raise RuntimeError("源文件复制期间发生变化")
        return written, digest.hexdigest()
    finally:
        os.close(descriptor)


def _temporary(root: Path, document: dict[str, Any], token: str) -> Path:
    """临时文件名包含冻结令牌，崩溃恢复只能删除可证明属于该声明的文件。"""
    return root / f".{document['id']}-{document['version']}.{token}.partial"


def _final(root: Path, document: dict[str, Any], token: str) -> Path:
    """最终文件也绑定令牌，过期声明绝不触及后来冻结得到的副本。"""
    return root / f"{document['id']}-{document['version']}.{token}.core"


def _owned_path(root: Path, value: str | None, token: str) -> Path | None:
    """仅接受受控目录内且带有当前令牌的临时文件路径。"""
    if not value:
        return None
    path = Path(value).resolve(strict=False)
    return path if path.parent == root and path.name.endswith(f".{token}.partial") else None


async def _reserve(repo: Any, token: str, file_id: str, size: int) -> None:
    """按令牌预留非零容量；活动令牌使重复释放不会把 used 减成负数。"""
    if size <= 0:
        raise ValueError("零字节 coredump 不创建快照配额声明")
    node_id, stamp, quota = repo.settings.node_id, now(), int(repo.settings.coredump_snapshot_quota_bytes)

    async def commit(session):
        existing = await repo.db.coredump_snapshot_claims.find_one({"id": token}, session=session)
        if existing and existing.get("state") in {"RESERVED", "PUBLISHED"}:
            return True
        await repo.db.coredump_snapshot_reservations.update_one(
            {"id": node_id},
            {"$setOnInsert": {"used": 0, "activeTokens": [], "updatedAt": stamp}},
            upsert=True,
            session=session,
        )
        reserved = await repo.db.coredump_snapshot_reservations.update_one(
            {"id": node_id, "used": {"$lte": quota - size}, "activeTokens": {"$ne": token}},
            {"$inc": {"used": size}, "$addToSet": {"activeTokens": token}, "$set": {"updatedAt": stamp}},
            session=session,
        )
        if reserved.matched_count != 1:
            return False
        await repo.db.coredump_snapshot_claims.update_one(
            {"id": token},
            {
                "$set": {
                    "nodeId": node_id,
                    "fileId": file_id,
                    "bytes": size,
                    "state": "RESERVED",
                    "expiresAt": stamp + timedelta(seconds=RESERVATION_LEASE_SECONDS),
                    "updatedAt": stamp,
                },
                "$setOnInsert": {"id": token, "createdAt": stamp},
            },
            upsert=True,
            session=session,
        )
        return True

    try:
        confirmed = await audited_mutations.mutation_transaction(repo, commit)
    except Exception:
        claim = await repo.db.coredump_snapshot_claims.find_one({"id": token, "nodeId": node_id})
        aggregate = await repo.db.coredump_snapshot_reservations.find_one({"id": node_id}) or {}
        confirmed = bool(
            claim
            and claim.get("state") in {"RESERVED", "PUBLISHED"}
            and token in aggregate.get("activeTokens", [])
        )
        if not confirmed:
            raise
    if not confirmed:
        raise OverflowError("节点 coredump 快照配额不足")


async def _release(repo: Any, token: str) -> None:
    """按活动令牌精确归还一次容量；重试不会重复扣减。"""
    node_id = repo.settings.node_id

    async def commit(session):
        claim = await repo.db.coredump_snapshot_claims.find_one(
            {"id": token, "nodeId": node_id}, session=session
        )
        size = int(claim.get("bytes", 0)) if claim else 0
        if size <= 0 or claim.get("state") == "RELEASED":
            return True
        released = await repo.db.coredump_snapshot_reservations.update_one(
            {"id": node_id, "activeTokens": token, "used": {"$gte": size}},
            {"$inc": {"used": -size}, "$pull": {"activeTokens": token}, "$set": {"updatedAt": now()}},
            session=session,
        )
        if not released.modified_count:
            return False
        await repo.db.coredump_snapshot_claims.update_one(
            {"id": token}, {"$set": {"state": "RELEASED", "releasedAt": now()}}, session=session
        )
        return True

    try:
        confirmed = await audited_mutations.mutation_transaction(repo, commit)
    except Exception:
        claim = await repo.db.coredump_snapshot_claims.find_one({"id": token, "nodeId": node_id}) or {}
        aggregate = await repo.db.coredump_snapshot_reservations.find_one({"id": node_id}) or {}
        confirmed = claim.get("state") == "RELEASED" and token not in aggregate.get("activeTokens", [])
        if not confirmed:
            raise
    if not confirmed:
        raise RuntimeError("coredump 快照配额声明无法释放")


async def _claim(repo: Any, document: dict[str, Any], root: Path) -> tuple[str, Path] | None:
    """以新的不透明令牌独占 FREEZING；仅 RECEIVING 版本可被领取。"""
    token, stamp = uuid.uuid4().hex, now()
    temporary = _temporary(root, document, token)
    changed = await repo.db.coredump_files.update_one(
        {
            "id": document["id"],
            "nodeId": repo.settings.node_id,
            "status": "RECEIVING",
            "source": document["source"],
            "snapshot": {"$exists": False},
        },
        {
            "$set": {
                "status": "FREEZING",
                "freezeToken": token,
                "freezeStartedAt": stamp,
                "freezeLeaseUntil": stamp + timedelta(seconds=FREEZE_LEASE_SECONDS),
                "freezeTemporaryPath": str(temporary),
                "updatedAt": stamp,
            }
        },
    )
    return (token, temporary) if changed.modified_count else None


async def freeze(repo: Any, document: dict[str, Any]) -> dict[str, Any]:
    """创建不可变副本并以令牌 CAS 发布；并发调用返回已确认的同版副本。"""
    if document.get("nodeId") != repo.settings.node_id:
        raise FileNotFoundError(document["id"])
    if document.get("status") == "FROZEN" and document.get("snapshot"):
        return document
    root = snapshots_root(repo.settings)
    claim = await _claim(repo, document, root)
    if claim is None:
        current = await repo.db.coredump_files.find_one({"id": document["id"]})
        if (
            current
            and current.get("status") == "FROZEN"
            and current.get("snapshot")
            and current.get("source") == document.get("source")
        ):
            return current
        raise RuntimeError("coredump 正在冻结或版本已变化")
    token, temporary = claim
    descriptor: int | None = None
    copying: asyncio.Task[tuple[int, str]] | None = None
    reserved = False
    final = _final(root, document, token)
    try:
        limit = int(repo.settings.coredump_snapshot_max_bytes)
        descriptor = open_source(repo.settings, document["deviceIp"], document["name"])
        source_info = os.fstat(descriptor)
        if source_info.st_size <= 0:
            raise ValueError("零字节 coredump 不创建快照")
        if source_info.st_size > limit:
            raise OverflowError("单个 coredump 超过节点快照上限")
        await _reserve(repo, token, document["id"], source_info.st_size)
        reserved = True
        from camera_logs.logs.job_threads import job_thread

        handoff = descriptor
        descriptor = None
        copying = asyncio.create_task(job_thread(_copy_stable, handoff, temporary, document["source"], limit))
        while not copying.done():
            done, _pending = await asyncio.wait({copying}, timeout=FREEZE_LEASE_SECONDS / 3)
            if done:
                break
            renewed = await repo.db.coredump_files.update_one(
                {"id": document["id"], "status": "FREEZING", "freezeToken": token},
                {
                    "$set": {
                        "freezeLeaseUntil": now() + timedelta(seconds=FREEZE_LEASE_SECONDS),
                        "updatedAt": now(),
                    }
                },
            )
            if renewed.modified_count != 1:
                await copying
                raise RuntimeError("coredump 冻结租约已失效")
        size, digest = await copying
        os.replace(temporary, final)
    except BaseException:
        # job_thread 会在取消后等待线程退出；必须先等它停止写临时文件才能回收声明。
        if copying is not None and not copying.done():
            copying.cancel()
            try:
                await asyncio.shield(copying)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("coredump 冻结取消后复制线程收尾异常")
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        if reserved:
            await _release(repo, token)
        await repo.db.coredump_files.update_one(
            {"id": document["id"], "status": "FREEZING", "freezeToken": token},
            {
                "$set": {"status": "RECEIVING", "updatedAt": now()},
                "$unset": {
                    "freezeToken": "",
                    "freezeStartedAt": "",
                    "freezeLeaseUntil": "",
                    "freezeTemporaryPath": "",
                },
            },
        )
        raise
    snapshot = {
        "path": str(final),
        "size": size,
        "sha256": digest,
        "etag": f'"{digest}"',
        "reservationToken": token,
    }
    published = await repo.db.coredump_files.update_one(
        {
            "id": document["id"],
            "status": "FREEZING",
            "freezeToken": token,
            "source": document["source"],
            "snapshot": {"$exists": False},
        },
        {
            "$set": {"snapshot": snapshot, "status": "FROZEN", "updatedAt": now()},
            "$unset": {
                "freezeToken": "",
                "freezeStartedAt": "",
                "freezeLeaseUntil": "",
                "freezeTemporaryPath": "",
            },
        },
    )
    if published.modified_count:
        await repo.db.coredump_snapshot_claims.update_one(
            {
                "id": token,
            },
            {
                "$set": {
                    "state": "PUBLISHED",
                    "publishedAt": now(),
                    "expiresAt": now() + timedelta(hours=repo.settings.coredump_retention_hours),
                }
            },
        )
        return await repo.db.coredump_files.find_one({"id": document["id"], "status": "FROZEN"})
    current = await repo.db.coredump_files.find_one({"id": document["id"]})
    if (
        current
        and current.get("status") == "FROZEN"
        and current.get("snapshot", {}).get("reservationToken") == token
    ):
        return current
    final.unlink(missing_ok=True)
    await _release(repo, token)
    raise RuntimeError("coredump 版本在快照发布前已变化")


async def reconcile_snapshots(repo: Any, *, timestamp: datetime | None = None) -> int:
    """回收本节点过期 FREEZING 租约和未发布配额，不删除 NFS 源文件。"""
    stamp, root, recovered = timestamp or now(), snapshots_root(repo.settings), 0
    async for document in repo.db.coredump_files.find(
        {"nodeId": repo.settings.node_id, "status": "FREEZING"}
    ):
        token = document.get("freezeToken")
        lease_until = document.get("freezeLeaseUntil")
        if not token or not lease_until or not _expired(lease_until, stamp):
            continue
        changed = await repo.db.coredump_files.update_one(
            {
                "id": document["id"],
                "status": "FREEZING",
                "freezeToken": token,
                "freezeLeaseUntil": lease_until,
            },
            {
                "$set": {"status": "RECEIVING", "updatedAt": stamp},
                "$unset": {
                    "freezeToken": "",
                    "freezeStartedAt": "",
                    "freezeLeaseUntil": "",
                    "freezeTemporaryPath": "",
                },
            },
        )
        if changed.modified_count:
            owned = _owned_path(root, document.get("freezeTemporaryPath"), token)
            if owned:
                owned.unlink(missing_ok=True)
            _final(root, document, token).unlink(missing_ok=True)
            await _release(repo, token)
            recovered += 1
    async for claim in repo.db.coredump_snapshot_claims.find(
        {"nodeId": repo.settings.node_id, "state": "RESERVED"}
    ):
        expires_at = claim.get("expiresAt")
        if not expires_at or not _expired(expires_at, stamp):
            continue
        file = await repo.db.coredump_files.find_one({"id": claim["fileId"]})
        if (
            file
            and file.get("status") == "FROZEN"
            and file.get("snapshot", {}).get("reservationToken") == claim["id"]
        ):
            await repo.db.coredump_snapshot_claims.update_one(
                {"id": claim["id"], "state": "RESERVED"},
                {
                    "$set": {
                        "state": "PUBLISHED",
                        "publishedAt": stamp,
                        "expiresAt": stamp + timedelta(hours=repo.settings.coredump_retention_hours),
                    }
                },
            )
            continue
        active_freeze = (
            file
            and file.get("status") == "FREEZING"
            and file.get("freezeToken") == claim["id"]
            and file.get("freezeLeaseUntil")
            and _active_after(file["freezeLeaseUntil"], stamp)
        )
        if not active_freeze:
            await _release(repo, claim["id"])
    async for document in repo.db.coredump_files.find(
        {
            "nodeId": repo.settings.node_id,
            "status": "FROZEN",
            "snapshot.reservationToken": {"$exists": True},
        }
    ):
        token = document["snapshot"]["reservationToken"]
        claim = await repo.db.coredump_snapshot_claims.find_one({"id": token, "state": "PUBLISHED"})
        if claim and claim.get("expiresAt") and _expired(claim["expiresAt"], stamp):
            await release_snapshot(repo, document)
    from camera_logs.coredumps.snapshot_lifecycle import reconcile_snapshots as reconcile_lifecycle
    return recovered + await reconcile_lifecycle(repo, timestamp=stamp)


async def release_snapshot(repo: Any, document: dict[str, Any]) -> bool:
    """兼容既有入口，实际退休状态机在独立 lifecycle 模块中实现。"""
    from camera_logs.coredumps.snapshot_lifecycle import release_snapshot as retire
    return await retire(repo, document)


def write_zip(destination: Path, files: list[tuple[str, Path]], limit: int) -> int:
    """用 ZIP STORE 顺序写入已冻结副本，不压缩也不整体加载到内存。"""
    written = 0
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as output:
        for name, source in files:
            with source.open("rb") as reader, output.open(name, "w", force_zip64=True) as member:
                while chunk := reader.read(1024 * 1024):
                    written += len(chunk)
                    if written > limit:
                        raise OverflowError("coredump 导出超过产物上限")
                    read_limiter.consume(len(chunk))
                    member.write(chunk)
    return destination.stat().st_size
