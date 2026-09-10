"""孤儿快照 claim 的精确 token 回收回归。"""

import asyncio
import threading
from datetime import timedelta

import pytest
from camera_logs.common.database import now
from camera_logs.coredumps.safety import snapshots_root
from camera_logs.coredumps.snapshot_orphans import reconcile_orphans
from test_coredump_snapshot_races import repository


async def test_orphan_reclaim_only_removes_matching_token_and_keeps_nfs_source(tmp_path):
    """旧 claim 只能删除精确 token 文件，替代 token、未登记文件和 NFS 源均保留。"""
    repo = await repository(tmp_path)
    root, stamp = snapshots_root(repo.settings), now()
    owned = root / "file-1.old.core"; replacement = root / "file-1.new.core"; unrelated = root / "keep.core"
    owned.write_bytes(b"old"); replacement.write_bytes(b"new"); unrelated.write_bytes(b"keep")
    source = repo.settings.nfs_root / "192.0.2.99" / "source.core"
    source.parent.mkdir(); source.write_bytes(b"source")
    await repo.db.coredump_snapshot_reservations.insert_one({"id": "node-a", "used": 3, "activeTokens": ["old"]})
    await repo.db.coredump_snapshot_claims.insert_one({"id": "old", "nodeId": "node-a", "fileId": "file", "version": 1,
                                                        "bytes": 3, "state": "RESERVED", "expiresAt": stamp - timedelta(seconds=1)})
    assert await reconcile_orphans(repo, timestamp=stamp) == 1
    assert not owned.exists() and replacement.exists() and unrelated.exists() and source.read_bytes() == b"source"


async def test_reclaim_failure_keeps_claim_and_does_not_block_next(tmp_path, monkeypatch):
    """单个 unlink 失败保留 RECLAIMING，下一 claim 仍可删除并释放。"""
    from camera_logs.coredumps import snapshot_orphans

    repo = await repository(tmp_path)
    root, stamp = snapshots_root(repo.settings), now()
    for token in ("bad", "good"):
        (root / f"file-1.{token}.core").write_bytes(token.encode())
        await repo.db.coredump_snapshot_claims.insert_one({"id": token, "nodeId": "node-a", "fileId": "file", "version": 1,
                                                            "bytes": 1, "state": "RESERVED", "expiresAt": stamp - timedelta(seconds=1)})
    await repo.db.coredump_snapshot_reservations.insert_one({"id": "node-a", "used": 2, "activeTokens": ["bad", "good"]})
    original = snapshot_orphans._delete_owned

    def fail_bad(root, claim):
        if claim["id"] == "bad":
            raise OSError("denied")
        original(root, claim)

    monkeypatch.setattr(snapshot_orphans, "_delete_owned", fail_bad)
    assert await reconcile_orphans(repo, timestamp=stamp) == 1
    assert (await repo.db.coredump_snapshot_claims.find_one({"id": "bad"}))["state"] == "RECLAIMING"
    assert (await repo.db.coredump_snapshot_claims.find_one({"id": "good"}))["state"] == "RELEASED"
    monkeypatch.setattr(snapshot_orphans, "_delete_owned", original)
    assert await reconcile_orphans(repo, timestamp=stamp) == 1
    assert not (root / "file-1.bad.core").exists()
    assert (await repo.db.coredump_snapshot_reservations.find_one({"id": "node-a"}))["used"] == 0


async def test_catalog_references_and_symlink_are_never_reclaimed(tmp_path):
    """任意活动 catalog token 引用或符号链接均阻止孤儿删除和配额释放。"""
    repo = await repository(tmp_path)
    root, stamp = snapshots_root(repo.settings), now()
    for token, status, field in (("freezing", "FREEZING", "freezeToken"), ("frozen", "FROZEN", "snapshot.reservationToken"), ("retiring", "RETIRING", "snapshot.reservationToken"), ("deleting", "DELETING", "snapshot.reservationToken")):
        document = {"id": token, "nodeId": "node-a", "status": status}
        if field == "freezeToken": document[field] = token
        else: document["snapshot"] = {"reservationToken": token}
        await repo.db.coredump_files.insert_one(document)
        await repo.db.coredump_snapshot_claims.insert_one({"id": token, "nodeId": "node-a", "fileId": "file", "version": 1, "bytes": 1, "state": "RESERVED", "expiresAt": stamp - timedelta(seconds=1)})
    link = root / "file-1.link.core"; link.symlink_to(root / "target")
    await repo.db.coredump_snapshot_claims.insert_one({"id": "link", "nodeId": "node-a", "fileId": "file", "version": 1, "bytes": 1, "state": "RESERVED", "expiresAt": stamp - timedelta(seconds=1)})
    assert await reconcile_orphans(repo, timestamp=stamp) == 0
    assert (await repo.db.coredump_snapshot_claims.find_one({"id": "link"}))["state"] == "RECLAIMING"


async def test_cancelled_reclaimer_waits_for_file_thread_and_retries_quota(tmp_path, monkeypatch):
    """取消维护时等待删除线程退出，配额保留至下一轮确认物理清理后释放。"""
    from camera_logs.coredumps import snapshot_orphans
    from camera_logs.coredumps.snapshots import _reserve

    repo = await repository(tmp_path)
    root, stamp = snapshots_root(repo.settings), now()
    await _reserve(repo, "cancel", "file", 3, version=1)
    await repo.db.coredump_snapshot_claims.update_one(
        {"id": "cancel"}, {"$set": {"expiresAt": stamp - timedelta(seconds=1)}},
    )
    path = root / "file-1.cancel.core"
    path.write_bytes(b"old")
    entered, unblock = asyncio.Event(), threading.Event()
    loop, original = asyncio.get_running_loop(), snapshot_orphans._delete_owned

    def blocked_delete(root, claim):
        loop.call_soon_threadsafe(entered.set)
        if not unblock.wait(5):
            raise TimeoutError("测试未释放删除线程")
        original(root, claim)

    monkeypatch.setattr(snapshot_orphans, "_delete_owned", blocked_delete)
    task = asyncio.create_task(reconcile_orphans(repo, timestamp=stamp))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and path.exists()
        assert (await repo.db.coredump_snapshot_claims.find_one({"id": "cancel"}))["state"] == "RECLAIMING"
    finally:
        unblock.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and not path.exists()
    assert (await repo.db.coredump_snapshot_reservations.find_one({"id": "node-a"}))["used"] == 3
    monkeypatch.setattr(snapshot_orphans, "_delete_owned", original)
    assert await reconcile_orphans(repo, timestamp=stamp) == 1
    assert (await repo.db.coredump_snapshot_reservations.find_one({"id": "node-a"}))["used"] == 0


@pytest.mark.parametrize("changed", [{"fileId": "../file"}, {"id": ""}, {"version": True}, {"version": -1}])
async def test_invalid_claim_identity_retains_quota(tmp_path, changed):
    """无法证明路径归属的声明只能保留待处理，不能把未删文件视为已经回收。"""
    repo = await repository(tmp_path)
    document = {"id": "invalid", "nodeId": "node-a", "fileId": "file", "version": 1,
                "bytes": 3, "state": "RESERVED", "expiresAt": now() - timedelta(seconds=1)} | changed
    await repo.db.coredump_snapshot_claims.insert_one(document)
    await repo.db.coredump_snapshot_reservations.insert_one(
        {"id": "node-a", "used": 3, "activeTokens": [document["id"]]},
    )
    assert await reconcile_orphans(repo) == 0
    assert (await repo.db.coredump_snapshot_claims.find_one({"id": document["id"]}))["state"] == "RECLAIMING"
    assert (await repo.db.coredump_snapshot_reservations.find_one({"id": "node-a"}))["used"] == 3


async def test_other_node_and_unexpired_claims_are_not_reclaimed(tmp_path):
    """当前节点维护不能处理别的节点文件，也不能提前回收未到期的声明。"""
    repo = await repository(tmp_path)
    for token, node, expiry in (("remote", "node-b", -1), ("future", "node-a", 3600)):
        await repo.db.coredump_snapshot_claims.insert_one(
            {"id": token, "nodeId": node, "fileId": "file", "version": 1, "bytes": 3,
             "state": "RESERVED", "expiresAt": now() + timedelta(seconds=expiry)},
        )
    assert await reconcile_orphans(repo) == 0
    assert await repo.db.coredump_snapshot_claims.count_documents({"state": "RESERVED"}) == 2
