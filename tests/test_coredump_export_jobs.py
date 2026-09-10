"""coredump 导出容量声明与生命周期回归，不访问真实设备或 NFS。"""

import asyncio

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
