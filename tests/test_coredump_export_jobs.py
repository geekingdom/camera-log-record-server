"""coredump 导出容量声明与生命周期回归，不访问真实设备或 NFS。"""

import asyncio
from datetime import timedelta

import pytest


async def test_failed_job_does_not_renew_lease(tmp_path):
    """已失败作业不可继续复制或续租，即便执行实例标识相同。"""
    from camera_logs.coredumps.jobs import _cancelled
    from test_coredump_storage import repository

    repo = await repository(tmp_path)
    job = {"id": "failed", "workerInstanceId": "worker", "status": "FAILED"}
    await repo.db.coredump_exports.insert_one(job)
    assert await _cancelled(repo, job)
    assert "leaseUntil" not in await repo.db.coredump_exports.find_one({"id": "failed"})
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.coredumps.jobs import _release, _reserve
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def repository(tmp_path, quota: int) -> Repository:
    """创建仅供导出配额测试使用的独立节点仓库。"""
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path / "logs",
                        nfs_root=tmp_path / "nfs", node_id="node-a", coredump_export_quota_bytes=quota)
    repo = Repository(AsyncMongoMockClient().db, settings)
    await repo.initialize()
    return repo


async def test_concurrent_export_claims_never_exceed_node_quota(tmp_path):
    """两个并发声明争抢只能容纳一份的额度时，最多一份成功。"""
    repo = await repository(tmp_path, 70_000)
    jobs = [{"id": "first", "estimatedBytes": 1, "sources": []},
            {"id": "second", "estimatedBytes": 1, "sources": []}]
    outcomes = await asyncio.gather(*(_reserve(repo, job) for job in jobs), return_exceptions=True)
    assert sum(isinstance(item, int) for item in outcomes) == 1
    total = await repo.db.coredump_export_reservations.find_one({"id": "node-a"})
    assert total["used"] <= 70_000


async def test_claim_rejects_amount_larger_than_quota(tmp_path):
    """预估正文、暂存和 ZIP 元数据开销超过节点额度时，尚未创建 claim。"""
    repo = await repository(tmp_path, 65_000)
    with pytest.raises(OverflowError):
        await _reserve(repo, {"id": "too-large", "estimatedBytes": 1, "sources": []})
    assert await repo.db.coredump_export_reservation_claims.count_documents({}) == 0


async def test_release_claim_is_exactly_once(tmp_path):
    """重复释放同一 TTL 遗留 claim 不会重复扣减节点总用量。"""
    repo = await repository(tmp_path, 100_000)
    await _reserve(repo, {"id": "export", "estimatedBytes": 1, "sources": []})
    assert await _release(repo, "export") is True
    assert await _release(repo, "export") is False
    total = await repo.db.coredump_export_reservations.find_one({"id": "node-a"})
    assert total["used"] == 0


async def test_cancelled_writer_keeps_claim_until_same_instance_finishes(tmp_path, monkeypatch):
    """取消只表达意图；同实例仍在写入时维护不得删 scratch 或释放 claim。"""
    from camera_logs.common.database import now
    from camera_logs.coredumps import jobs

    repo = await repository(tmp_path, 200_000)
    source = {"id": "file", "nodeId": "node-a", "resourceId": "camera", "version": 1, "source": {"size": 1}}
    await repo.db.coredump_files.insert_one(source | {"name": "core", "size": 1})
    job = {"id": "export", "actor": "verify", "status": "RUNNING", "workerInstanceId": "worker",
           "estimatedBytes": 1, "sources": [source], "leaseUntil": now() + timedelta(seconds=90)}
    await repo.db.coredump_exports.insert_one(job)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocking_freeze(_repo, file):
        entered.set()
        await release.wait()
        return file

    monkeypatch.setattr(jobs, "_freeze", blocking_freeze)
    task = asyncio.create_task(jobs.run_export(repo, job))
    await asyncio.wait_for(entered.wait(), 1)
    scratch = repo.settings.log_root / "exports" / "coredumps" / ".tmp" / "export"
    assert scratch.exists()
    await repo.db.coredump_exports.update_one({"id": "export"}, {"$set": {"status": "CANCELLED"}})
    assert await jobs.cleanup_expired_exports(repo) == 0
    assert scratch.exists()
    claim = await repo.db.coredump_export_reservation_claims.find_one({"id": "export"})
    assert claim["state"] == "RESERVED"
    release.set()
    assert (await task)["status"] == "CANCELLED"
    document = await repo.db.coredump_exports.find_one({"id": "export"})
    assert document["executionState"] == "FINISHED"
    assert not scratch.exists()
    claim = await repo.db.coredump_export_reservation_claims.find_one({"id": "export"})
    assert claim["state"] == "RELEASED"


async def test_cancel_before_execution_releases_existing_claim(tmp_path):
    """取消先于执行声明到达时，不写文件也必须回收已存在的 claim。"""
    from camera_logs.coredumps.jobs import run_export

    repo = await repository(tmp_path, 200_000)
    job = {"id": "cancelled", "actor": "verify", "status": "CANCELLED", "workerInstanceId": "worker",
           "estimatedBytes": 1, "sources": []}
    await repo.db.coredump_exports.insert_one(job)
    await _reserve(repo, job)
    assert (await run_export(repo, job))["status"] == "CANCELLED"
    claim = await repo.db.coredump_export_reservation_claims.find_one({"id": "cancelled"})
    assert claim["state"] == "RELEASED"


async def test_cancelled_writer_renews_physical_lease_until_cleanup(tmp_path):
    """业务已取消不应停止 WRITING 租约，否则长文件清理会被维护并发删除。"""
    from camera_logs.common.database import now
    from camera_logs.coredumps.jobs import _renew_execution_lease

    repo = await repository(tmp_path, 200_000)
    expired = now() - timedelta(seconds=1)
    job = {"id": "physical", "workerInstanceId": "worker"}
    await repo.db.coredump_exports.insert_one(job | {"status": "CANCELLED", "executionState": "WRITING",
                                                      "executionLeaseUntil": expired})
    assert await _renew_execution_lease(repo, job)
    current = await repo.db.coredump_exports.find_one({"id": "physical"})
    for field in ("executionLeaseUntil", "leaseUntil"):
        lease = current[field]
        lease = lease.replace(tzinfo=now().tzinfo) if lease.tzinfo is None else lease
        assert lease > now()


async def test_failed_begin_does_not_touch_another_writer_claim_or_directory(tmp_path):
    """同 ID 已由其它实例写入时，迟到实例不能释放 claim 或清理其临时目录。"""
    from camera_logs.coredumps.jobs import run_export

    repo = await repository(tmp_path, 200_000)
    await _reserve(repo, {"id": "shared", "estimatedBytes": 1, "sources": []})
    scratch = repo.settings.log_root / "exports" / "coredumps" / ".tmp" / "shared"
    scratch.mkdir(parents=True)
    await repo.db.coredump_exports.insert_one({"id": "shared", "status": "RUNNING", "workerInstanceId": "owner",
                                                "executionState": "WRITING"})
    result = await run_export(repo, {"id": "shared", "workerInstanceId": "late", "sources": [], "estimatedBytes": 1})
    assert result["status"] == "CANCELLED"
    assert scratch.exists()
    claim = await repo.db.coredump_export_reservation_claims.find_one({"id": "shared"})
    assert claim["state"] == "RESERVED"
