"""节点低频维护在无 NFS 功能时的隔离回归。"""

import asyncio
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


async def test_blocked_archive_recovery_is_single_flight_and_does_not_block_tick(tmp_path, monkeypatch, caplog):
    """模拟历史恢复挂起时监督继续心跳，失败被记录且后续可重新恢复。"""
    worker, repo = await make_worker(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_maintenance():
        entered.set()
        await release.wait()
        raise OSError("synthetic old archive failure")

    monkeypatch.setattr(worker, "_maintain", blocked_maintenance)
    first = worker.start_maintenance()
    await entered.wait()
    try:
        worker.last_maintenance = 0
        await asyncio.wait_for(worker.tick(), timeout=1)
        assert worker.start_maintenance() is first
        assert (await repo.db.nodes.find_one({"id": repo.settings.node_id})) is not None
        assert not first.done()
        release.set()
        await first
        assert "节点归档恢复或后台维护失败" in caplog.text
        completed = AsyncMock()
        monkeypatch.setattr(worker, "_maintain", completed)
        second = worker.start_maintenance()
        assert second is not first
        await second
        completed.assert_awaited_once()
    finally:
        release.set()
        await worker.close()


async def test_archive_pump_runs_while_full_maintenance_is_blocked(tmp_path, monkeypatch):
    """历史 tar 恢复卡住时，独立 spool pump 仍按五秒周期取得新的调度机会。"""
    worker, _repo = await make_worker(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_maintenance():
        entered.set()
        await release.wait()

    pump = AsyncMock()
    monkeypatch.setattr(worker, "_maintain", blocked_maintenance)
    monkeypatch.setattr("camera_logs.logs.maintenance.pump_pending_archives", pump)
    maintenance = worker.start_maintenance()
    await entered.wait()
    try:
        pumping = worker.start_archive_pump()
        await pumping
        pump.assert_awaited_once_with(worker.repo)
        assert not maintenance.done()
    finally:
        release.set()
        await worker.close()
