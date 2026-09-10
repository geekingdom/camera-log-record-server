"""节点低频维护在无 NFS 功能时的隔离回归。"""

from unittest.mock import AsyncMock

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.worker import Worker
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def make_worker(tmp_path, *, nfs_server_ip=""):
    """构造独立数据库，避免测试读取开发机 NFS 与服务配置。"""
    repo = Repository(
        AsyncMongoMockClient().db,
        Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                 nfs_server_ip=nfs_server_ip),
    )
    await repo.initialize()
    return Worker(repo), repo


async def test_no_nfs_node_skips_snapshot_reconcile_and_keeps_existing_catalog(tmp_path, monkeypatch):
    """未启用 NFS 时仍清理日志/导出，但不处理可能来自历史的快照记录。"""
    worker, repo = await make_worker(tmp_path)
    await repo.db.coredump_files.insert_one({"id": "historical", "status": "FROZEN"})
    maintain, cleanup, reconcile = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr("camera_logs.logs.maintenance.maintain", maintain)
    monkeypatch.setattr("camera_logs.coredumps.jobs.cleanup_expired_exports", cleanup)
    monkeypatch.setattr("camera_logs.coredumps.snapshots.reconcile_snapshots", reconcile)

    await worker._maintain()

    maintain.assert_awaited_once_with(repo)
    cleanup.assert_awaited_once_with(repo)
    reconcile.assert_not_awaited()
    assert await repo.db.coredump_files.find_one({"id": "historical"}) is not None


async def test_nfs_node_keeps_snapshot_reconcile_failure_observable(tmp_path, monkeypatch):
    """启用 NFS 的节点继续运行快照回收，真实异常不能被无声吞掉。"""
    worker, repo = await make_worker(tmp_path, nfs_server_ip="192.0.2.10")
    maintain, cleanup = AsyncMock(), AsyncMock()
    failure = OSError("synthetic snapshot failure")
    reconcile = AsyncMock(side_effect=failure)
    monkeypatch.setattr("camera_logs.logs.maintenance.maintain", maintain)
    monkeypatch.setattr("camera_logs.coredumps.jobs.cleanup_expired_exports", cleanup)
    monkeypatch.setattr("camera_logs.coredumps.snapshots.reconcile_snapshots", reconcile)

    with pytest.raises(OSError, match="synthetic snapshot failure"):
        await worker._maintain()

    maintain.assert_awaited_once_with(repo)
    cleanup.assert_awaited_once_with(repo)
    reconcile.assert_awaited_once_with(repo)
