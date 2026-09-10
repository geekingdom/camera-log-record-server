"""冻结快照读者 lease 与退休删除状态机回归。"""

from pathlib import Path

import pytest
from camera_logs.coredumps.snapshot_lifecycle import reconcile_snapshots, release_snapshot
from camera_logs.coredumps.snapshot_readers import SnapshotReader
from camera_logs.coredumps.snapshots import freeze
from test_coredump_snapshot_races import _catalog, repository


async def test_retiring_snapshot_waits_for_reader_then_deletes(tmp_path):
    """TTL 标记退休后保留已开始下载的副本，读者关闭才删除并释放 quota。"""
    repo = await repository(tmp_path)
    entry = await _catalog(repo, "192.0.2.90", "core.bin", b"payload")
    frozen = await freeze(repo, entry)
    path = frozen["snapshot"]["path"]
    reader = await SnapshotReader(repo, frozen).acquire()
    assert await release_snapshot(repo, frozen)
    assert await reconcile_snapshots(repo) == 0
    assert Path(path).exists()
    await reader.close()
    assert await reconcile_snapshots(repo) == 1
    current = await repo.db.coredump_files.find_one({"id": entry["id"]})
    assert current["status"] == "RECEIVING" and not Path(path).exists()


async def test_retiring_snapshot_rejects_new_reader_and_close_is_idempotent(tmp_path):
    """RETIRING 阻止新下载；旧 reader 重复 close 不会影响其它 catalog 字段。"""
    repo = await repository(tmp_path)
    entry = await _catalog(repo, "192.0.2.91", "core.bin", b"payload")
    frozen = await freeze(repo, entry)
    held = await SnapshotReader(repo, frozen).acquire()
    assert await release_snapshot(repo, frozen)
    newcomer = SnapshotReader(repo, frozen)
    with pytest.raises(RuntimeError):
        await newcomer.acquire()
    await held.close()
    await held.close()


async def test_reader_uses_snapshot_node_not_coordinator_node(tmp_path):
    """跨节点导出协调器可在共享 catalog 上 pin 持有快照的远端节点版本。"""
    repo = await repository(tmp_path)
    document = {"id": "remote-file", "nodeId": "remote-node", "status": "FROZEN",
                "snapshot": {"reservationToken": "token", "path": "/private/ignored"}}
    await repo.db.coredump_files.insert_one(document)
    reader = await SnapshotReader(repo, document).acquire()
    current = await repo.db.coredump_files.find_one({"id": "remote-file"})
    assert current["snapshotReaders"][0]["id"] == reader.identifier
    await reader.close()


async def test_retiring_allows_child_only_for_valid_parent_reader(tmp_path):
    """远端 child 只能由尚有效的协调端父 reader 委托进入 RETIRING 副本。"""
    repo = await repository(tmp_path)
    entry = await _catalog(repo, "192.0.2.92", "core.bin", b"payload")
    frozen = await freeze(repo, entry)
    parent = await SnapshotReader(repo, frozen).acquire()
    assert await release_snapshot(repo, frozen)
    child = await SnapshotReader(repo, frozen, parent_id=parent.identifier).acquire()
    await child.close()
    with pytest.raises(RuntimeError):
        await SnapshotReader(repo, frozen, parent_id="unknown").acquire()
    await parent.close()


async def test_retiring_child_renews_after_parent_lease_expires(tmp_path):
    """父 reader 仅授权 child 获取；child 已持有自身 lease 后可独立完成传输。"""
    from camera_logs.common.database import now

    repo = await repository(tmp_path)
    entry = await _catalog(repo, "192.0.2.93", "core.bin", b"payload")
    frozen = await freeze(repo, entry)
    parent = await SnapshotReader(repo, frozen).acquire()
    assert await release_snapshot(repo, frozen)
    child = await SnapshotReader(repo, frozen, parent_id=parent.identifier).acquire()
    await repo.db.coredump_files.update_one({"id": entry["id"], "snapshotReaders.id": parent.identifier},
                                             {"$set": {"snapshotReaders.$.expiresAt": now()}})
    assert await child._renew_once()
    await child.close()
    await parent.close()


async def test_unlink_failure_keeps_deleting_catalog_for_recovery(tmp_path, monkeypatch):
    """unlink 失败时 snapshot/token 和配额仍保留，后续 reconcile 可以安全重试。"""
    from camera_logs.coredumps import snapshot_lifecycle

    repo = await repository(tmp_path)
    entry = await _catalog(repo, "192.0.2.94", "core.bin", b"payload")
    frozen = await freeze(repo, entry)
    original = snapshot_lifecycle.Path.unlink

    def failed_unlink(_path, *args, **kwargs):
        raise OSError("disk error")

    monkeypatch.setattr(snapshot_lifecycle.Path, "unlink", failed_unlink)
    with pytest.raises(OSError, match="disk error"):
        await release_snapshot(repo, frozen)
    current = await repo.db.coredump_files.find_one({"id": entry["id"]})
    assert current["status"] == "DELETING" and current["snapshot"]["reservationToken"] == frozen["snapshot"]["reservationToken"]
    monkeypatch.setattr(snapshot_lifecycle.Path, "unlink", original)
    assert await reconcile_snapshots(repo) == 1


async def test_release_failure_after_unlink_recovers_without_losing_catalog(tmp_path, monkeypatch):
    """副本已删除但配额事务失败时保留 DELETING，恢复轮次完成 release 与 catalog 收尾。"""
    from camera_logs.coredumps import snapshots

    repo = await repository(tmp_path)
    entry = await _catalog(repo, "192.0.2.95", "core.bin", b"payload")
    frozen = await freeze(repo, entry)
    original = snapshots._release

    async def failed_release(*_args):
        raise RuntimeError("reservation unavailable")

    monkeypatch.setattr(snapshots, "_release", failed_release)
    with pytest.raises(RuntimeError, match="reservation unavailable"):
        await release_snapshot(repo, frozen)
    current = await repo.db.coredump_files.find_one({"id": entry["id"]})
    assert current["status"] == "DELETING"
    monkeypatch.setattr(snapshots, "_release", original)
    assert await reconcile_snapshots(repo) == 1


async def test_stale_retire_token_cannot_clear_new_snapshot(tmp_path):
    """旧 token 的晚到 DELETING 收尾不匹配新快照，不能删除或清空新版本。"""
    from camera_logs.common.database import now
    from camera_logs.coredumps.snapshot_lifecycle import _finish

    repo = await repository(tmp_path)
    entry = await _catalog(repo, "192.0.2.96", "core.bin", b"payload")
    old = await freeze(repo, entry)
    assert await release_snapshot(repo, old)
    current = await repo.db.coredump_files.find_one({"id": entry["id"]})
    newer = await freeze(repo, current)
    stale = old | {"status": "DELETING", "retireToken": old["snapshot"]["reservationToken"]}
    assert not await _finish(repo, stale, now())
    current = await repo.db.coredump_files.find_one({"id": entry["id"]})
    assert current["status"] == "FROZEN" and current["snapshot"]["reservationToken"] == newer["snapshot"]["reservationToken"]
