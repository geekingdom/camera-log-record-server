"""日志下载和检索作业的节点租约、崩溃终态恢复回归。"""

import asyncio
from datetime import timedelta

import pytest
from camera_logs.common import audited_mutations
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.logs import job_lease
from camera_logs.logs.job_lease import (
    JOB_LEASE_SECONDS,
    JOB_RENEW_SECONDS,
    _recover_expired_job,
    claim_job,
    recover_expired_jobs,
    renew_job,
)
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


def repository(tmp_path, node_id="node-a"):
    """构造只含内存 jobs collection 的本节点仓库，不读取或删除日志文件。"""
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                        node_id=node_id, start_background=False)
    return Repository(AsyncMongoMockClient().db, settings)


async def insert_job(repo, identifier, *, node_id="node-a", status="QUEUED", created_at=None, **extra):
    """建立最小安全作业夹具，明确 actor 与节点归属。"""
    await repo.db.jobs.insert_one({"id": identifier, "nodeId": node_id, "status": status,
                                   "actor": "requester", "createdAt": created_at or now()} | extra)


async def test_concurrent_claims_select_each_job_once_in_creation_order(tmp_path):
    """同节点并发领取不得重叠，较早作业先由第一个成功调用取得。"""
    repo = repository(tmp_path)
    earlier = now() - timedelta(seconds=1)
    await insert_job(repo, "first", created_at=earlier)
    await insert_job(repo, "second", created_at=earlier + timedelta(microseconds=1))
    claimed = await asyncio.gather(claim_job(repo, "worker-1"), claim_job(repo, "worker-2"))
    assert [item["id"] for item in claimed] == ["first", "second"]
    assert {item["workerInstanceId"] for item in claimed} == {"worker-1", "worker-2"}
    assert all(item["status"] == "RUNNING" and item["executionToken"] for item in claimed)
    assert all(item["leaseUntil"] > item["startedAt"] for item in claimed)
    assert JOB_LEASE_SECONDS == 90 and JOB_RENEW_SECONDS == 15


async def test_claim_is_limited_to_current_node(tmp_path):
    """本节点没有队列作业时不能领取其它节点的文件工作。"""
    repo = repository(tmp_path)
    await insert_job(repo, "remote", node_id="node-b")
    assert await claim_job(repo, "worker") is None
    assert (await repo.db.jobs.find_one({"id": "remote"}))["status"] == "QUEUED"


async def test_renew_requires_current_token_running_state_and_unexpired_lease(tmp_path):
    """错误 token、终态或已过期租约均不能被迟到 Worker 续活。"""
    repo = repository(tmp_path)
    claimed_at = now()
    await insert_job(repo, "job", status="RUNNING", executionToken="current", workerInstanceId="worker",
                     leaseUntil=claimed_at + timedelta(seconds=10), startedAt=claimed_at)
    current = await repo.db.jobs.find_one({"id": "job"})
    assert await renew_job(repo, current | {"executionToken": "wrong"}) is False
    assert await renew_job(repo, current) is True
    await repo.db.jobs.update_one({"id": "job"}, {"$set": {"status": "FAILED"}})
    assert await renew_job(repo, current) is False
    await repo.db.jobs.update_one({"id": "job"}, {"$set": {"status": "RUNNING", "leaseUntil": now() - timedelta(seconds=1)}})
    assert await renew_job(repo, current) is False


async def test_recovery_marks_only_expired_current_node_tokenized_jobs_failed(tmp_path):
    """恢复只终结当前节点已确知租约的遗留执行，不删除文件也不重跑作业。"""
    repo = repository(tmp_path)
    expired = now() - timedelta(seconds=1)
    await insert_job(repo, "expired", status="RUNNING", executionToken="token", workerInstanceId="lost",
                     leaseUntil=expired)
    await insert_job(repo, "active", status="RUNNING", executionToken="token", workerInstanceId="live",
                     leaseUntil=now() + timedelta(seconds=90))
    await insert_job(repo, "remote", node_id="node-b", status="RUNNING", executionToken="token",
                     workerInstanceId="remote", leaseUntil=expired)
    await insert_job(repo, "legacy", status="RUNNING")
    assert await recover_expired_jobs(repo) == 1
    failed = await repo.db.jobs.find_one({"id": "expired"})
    assert failed["status"] == "FAILED" and failed["error"] == "WORKER_EXECUTION_LOST"
    assert "执行" in failed["errorMessage"] and failed["completedAt"]
    assert "executionToken" not in failed and "leaseUntil" not in failed
    assert failed["workerInstanceId"] == "lost"
    assert (await repo.db.jobs.find_one({"id": "active"}))["status"] == "RUNNING"
    assert (await repo.db.jobs.find_one({"id": "remote"}))["status"] == "RUNNING"
    assert (await repo.db.jobs.find_one({"id": "legacy"}))["status"] == "RUNNING"
    assert await repo.db.audit.count_documents({"action": "job_failed", "targetId": "expired", "actor": "requester"}) == 1


async def test_recovery_audit_transaction_failure_does_not_mutate_job(tmp_path, monkeypatch):
    """事务未开始时的审计基础设施失败必须保持遗留作业原状。"""
    repo = repository(tmp_path)
    await insert_job(repo, "expired", status="RUNNING", executionToken="token", workerInstanceId="lost",
                     leaseUntil=now() - timedelta(seconds=1))

    async def unavailable(_repo, _callback):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(audited_mutations, "mutation_transaction", unavailable)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await _recover_expired_job(repo, await repo.db.jobs.find_one({"id": "expired"}), now())
    assert (await repo.db.jobs.find_one({"id": "expired"}))["status"] == "RUNNING"


async def test_recovery_cas_loses_to_concurrent_renewal(tmp_path, monkeypatch):
    """恢复扫描后的续租抢先发生时，旧 lease/token 条件不能覆盖有效执行。"""
    repo = repository(tmp_path)
    expired = now() - timedelta(seconds=1)
    await insert_job(repo, "race", status="RUNNING", executionToken="token", workerInstanceId="worker",
                     leaseUntil=expired)
    original = audited_mutations.mutation_transaction
    injected = False

    async def renew_before_terminal_cas(current_repo, callback):
        nonlocal injected
        if not injected:
            injected = True
            await current_repo.db.jobs.update_one(
                {"id": "race"}, {"$set": {"leaseUntil": now() + timedelta(seconds=90)}}
            )
        return await original(current_repo, callback)

    monkeypatch.setattr(audited_mutations, "mutation_transaction", renew_before_terminal_cas)
    assert await recover_expired_jobs(repo) == 0
    current = await repo.db.jobs.find_one({"id": "race"})
    assert injected and current["status"] == "RUNNING" and current["executionToken"] == "token"


async def test_global_recovery_handles_expired_remote_node_without_touching_active_job(tmp_path):
    """API 维护全局扫描可收尾离线节点的过期租约，仍保留未到期执行。"""
    repo = repository(tmp_path)
    await insert_job(repo, "remote-expired", node_id="node-b", status="RUNNING", executionToken="old",
                     workerInstanceId="lost", leaseUntil=now() - timedelta(seconds=1))
    await insert_job(repo, "remote-active", node_id="node-b", status="RUNNING", executionToken="live",
                     workerInstanceId="running", leaseUntil=now() + timedelta(seconds=90))
    assert await recover_expired_jobs(repo) == 0
    assert await recover_expired_jobs(repo, all_nodes=True) == 1
    assert (await repo.db.jobs.find_one({"id": "remote-expired"}))["status"] == "FAILED"
    assert (await repo.db.jobs.find_one({"id": "remote-active"}))["status"] == "RUNNING"


async def test_recovery_loop_scans_all_nodes_and_propagates_cancellation(tmp_path, monkeypatch):
    """API 后台循环必须使用全节点范围，并允许应用生命周期可靠取消。"""
    repo = repository(tmp_path)
    calls = []

    async def recovered(current_repo, *, all_nodes):
        assert current_repo is repo
        calls.append(all_nodes)

    async def stop_after_first_sleep(seconds):
        assert seconds == 30
        raise asyncio.CancelledError

    monkeypatch.setattr(job_lease, "recover_expired_jobs", recovered)
    monkeypatch.setattr(job_lease.asyncio, "sleep", stop_after_first_sleep)
    with pytest.raises(asyncio.CancelledError):
        await job_lease.recovery_loop(repo)
    assert calls == [True]
