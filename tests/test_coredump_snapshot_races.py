"""coredump 冻结租约、配额和有界扫描的竞争回归。"""

import os
import types
from datetime import timedelta

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.coredumps.safety import snapshots_root
from camera_logs.coredumps.scanner import _scan_directory
from camera_logs.coredumps.snapshots import freeze, reconcile_snapshots, release_snapshot
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def repository(tmp_path, *, maximum=20_000_000_000, quota=100_000_000_000):
    """构造隔离的节点仓库，不接触开发库、真实设备或导出目录。"""
    settings = Settings(
        _env_file=None,
        encryption_key=Fernet.generate_key().decode(),
        log_root=tmp_path / "logs",
        nfs_root=tmp_path / "nfs",
        coredump_snapshot_max_bytes=maximum,
        coredump_snapshot_quota_bytes=quota,
        coredump_scan_max_files=500,
        node_id="node-a",
    )
    settings.nfs_root.mkdir(parents=True)
    repo = Repository(AsyncMongoMockClient().db, settings)
    await repo.initialize()
    return repo


async def _catalog(repo, ip, name, content):
    """构造单个已扫描文件，所有测试只使用临时 NFS 根目录。"""
    directory = repo.settings.nfs_root / ip
    directory.mkdir()
    (directory / name).write_bytes(content)
    await repo.db.resources.insert_one(
        {"id": "camera", "ip": ip, "kind": "HIKVISION_NETWORK", "deletedAt": None}
    )
    from camera_logs.coredumps.scanner import CoredumpScanner

    await CoredumpScanner(repo).scan_once()
    return await repo.db.coredump_files.find_one({"resourceId": "camera", "name": name})


async def test_zero_byte_snapshot_is_rejected_without_quota_claim(tmp_path):
    """空文件不占用或制造可发布的零字节快照声明。"""
    repo = await repository(tmp_path)
    entry = await _catalog(repo, "192.0.2.71", "empty.core", b"")
    with pytest.raises(ValueError, match="零字节"):
        await freeze(repo, entry)
    assert not await repo.db.coredump_snapshot_claims.find_one({})
    assert (await repo.db.coredump_files.find_one({"id": entry["id"]}))["status"] == "RECEIVING"


async def test_stale_freezing_releases_only_owned_temporary_and_quota(tmp_path):
    """崩溃遗留令牌过期后仅删除受控 partial，源文件和总额均可恢复。"""
    repo = await repository(tmp_path)
    entry = await _catalog(repo, "192.0.2.72", "core.bin", b"payload")
    from camera_logs.coredumps.safety import snapshots_root

    root, token, stamp = snapshots_root(repo.settings), "expired-token", now()
    temporary = root / f".{entry['id']}-{entry['version']}.{token}.partial"
    temporary.write_bytes(b"partial")
    await repo.db.coredump_files.update_one(
        {"id": entry["id"]},
        {
            "$set": {
                "status": "FREEZING",
                "freezeToken": token,
                "freezeTemporaryPath": str(temporary),
                "freezeLeaseUntil": stamp - timedelta(seconds=1),
            }
        },
    )
    await repo.db.coredump_snapshot_reservations.insert_one(
        {"id": "node-a", "used": 7, "activeTokens": [token]}
    )
    await repo.db.coredump_snapshot_claims.insert_one(
        {
            "id": token,
            "nodeId": "node-a",
            "fileId": entry["id"],
            "bytes": 7,
            "state": "RESERVED",
            "expiresAt": stamp - timedelta(seconds=1),
        }
    )

    assert await reconcile_snapshots(repo, timestamp=stamp) == 1

    current = await repo.db.coredump_files.find_one({"id": entry["id"]})
    aggregate = await repo.db.coredump_snapshot_reservations.find_one({"id": "node-a"})
    assert current["status"] == "RECEIVING" and not temporary.exists()
    assert aggregate["used"] == 0 and token not in aggregate["activeTokens"]
    assert (repo.settings.nfs_root / "192.0.2.72" / "core.bin").read_bytes() == b"payload"


async def test_copy_handoff_never_closes_descriptor_twice(tmp_path, monkeypatch):
    """工作线程接管描述符后失败，冻结外层不能再次关闭已交还的编号。"""
    repo = await repository(tmp_path)
    entry = await _catalog(repo, "192.0.2.73", "core.bin", b"payload")
    from camera_logs.coredumps import snapshots

    original_close, closed = os.close, []

    def failing_copy(descriptor, _target, _expected, _limit):
        original_close(descriptor)
        raise RuntimeError("copy failed")

    def tracked_close(descriptor):
        closed.append(descriptor)
        return original_close(descriptor)

    monkeypatch.setattr(snapshots, "_copy_stable", failing_copy)
    monkeypatch.setattr(snapshots, "os", types.SimpleNamespace(close=tracked_close, fstat=os.fstat))
    with pytest.raises(RuntimeError, match="copy failed"):
        await freeze(repo, entry)
    # failing_copy 的 close 使用原函数；外层若误关会留下一个追踪到的 fd close。
    assert not closed


def test_directory_pending_stack_is_bounded(tmp_path):
    """宽目录不缓存兄弟路径，深度受限迭代仍可到达全部子目录。"""

    class Settings:
        nfs_root = tmp_path

    root = tmp_path / "192.0.2.74"
    root.mkdir()
    for index in range(80):
        child = root / f"dir-{index:03}"
        child.mkdir()
        (child / "core.bin").write_bytes(b"x")
    entries = _scan_directory(Settings(), {"ip": "192.0.2.74"}, "", 80)
    assert len(entries) == 80


async def test_releasing_published_snapshot_returns_file_to_receiving(tmp_path):
    """导出到期释放配额后保留目录记录，使仍存在的源可再次冻结。"""
    repo = await repository(tmp_path)
    entry = await _catalog(repo, "192.0.2.75", "core.bin", b"payload")
    frozen = await freeze(repo, entry)
    token = frozen["snapshot"]["reservationToken"]

    assert await release_snapshot(repo, frozen)
    assert not await release_snapshot(repo, frozen)
    assert await reconcile_snapshots(repo) == 0

    current = await repo.db.coredump_files.find_one({"id": entry["id"]})
    aggregate = await repo.db.coredump_snapshot_reservations.find_one({"id": "node-a"})
    assert current["status"] == "RECEIVING" and "snapshot" not in current
    assert aggregate["used"] == 0 and token not in aggregate["activeTokens"]


async def test_reconcile_expires_published_snapshot_and_allows_refreeze(tmp_path):
    """维护周期回收过期副本后，同一目录版本仍能由后续导出重新固定。"""
    repo = await repository(tmp_path)
    entry = await _catalog(repo, "192.0.2.76", "core.bin", b"payload")
    frozen = await freeze(repo, entry)
    token = frozen["snapshot"]["reservationToken"]
    stamp = now()
    await repo.db.coredump_snapshot_claims.update_one(
        {"id": token}, {"$set": {"expiresAt": stamp - timedelta(seconds=1)}}
    )

    assert await reconcile_snapshots(repo, timestamp=stamp) == 0

    released = await repo.db.coredump_files.find_one({"id": entry["id"]})
    assert released["status"] == "RECEIVING" and "snapshot" not in released
    assert (await freeze(repo, released))["status"] == "FROZEN"


def test_snapshot_root_rejects_directory_inside_nfs_export(tmp_path):
    """快照目录若落在设备可写导出树，必须拒绝该配置。"""

    class Settings:
        nfs_root = tmp_path / "nfs"
        log_root = nfs_root / "logs"

    Settings.nfs_root.mkdir()
    with pytest.raises(ValueError, match="不得位于 NFS 导出目录"):
        snapshots_root(Settings())
