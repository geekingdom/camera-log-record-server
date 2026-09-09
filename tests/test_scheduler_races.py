"""调度领取竞态：事务 CAS 不得覆盖停止或后继归属。"""

from datetime import timedelta

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

pytestmark = pytest.mark.usefixtures("mock_claim_transaction")


@pytest.mark.parametrize("desired,status", [("PAUSED", "PAUSED"), ("STOPPED", "STOPPED")])
async def test_no_capacity_does_not_overwrite_new_control(tmp_path, monkeypatch, desired, status):
    """容量不足的排队回写必须复核意图，不能覆盖并发暂停或停止的已完成状态。"""
    repo = Repository(AsyncMongoMockClient().db, Settings(
        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))
    await repo.initialize()
    await repo.db.tasks.insert_one({"id": "queued", "nodeId": None, "status": "STOPPED",
                                    "desiredState": "RUNNING", "generation": 0})
    collection_type = type(repo.db.tasks)
    original_update = collection_type.update_one
    intercepted = False

    async def control_before_queue_write(self, query, update, **kwargs):
        nonlocal intercepted
        if self.name == "tasks" and update.get("$set", {}).get("status") == "PENDING" and not intercepted:
            intercepted = True
            await original_update(self, {"id": "queued"},
                                  {"$set": {"status": status, "desiredState": desired}})
        return await original_update(self, query, update, **kwargs)

    monkeypatch.setattr(collection_type, "update_one", control_before_queue_write)
    await schedule_once(repo)
    task = await repo.get("tasks", "queued")
    assert intercepted and task["status"] == status and task["desiredState"] == desired


async def _repo_with_paused_task(tmp_path):
    """建立已暂停且保留原运行端点锁的 SSH 任务。"""
    repo = Repository(
        AsyncMongoMockClient().db,
        Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path),
    )
    await repo.initialize()
    await repo.db.nodes.insert_one(
        {"id": "node-a", "heartbeat": now(), "diskPercent": 10, "accepting": True, "capacity": 100}
    )
    await repo.db.tasks.insert_one(
        {
            "id": "paused-task",
            "protocol": "SSH",
            "ip": "127.0.0.1",
            "port": 22,
            "runId": "paused-run",
            "nodeId": None,
            "status": "PAUSED",
            "desiredState": "RUNNING",
            "generation": 1,
        }
    )
    await repo.db.endpoint_locks.insert_one(
        {"endpoint": "127.0.0.1:22", "taskId": "paused-task", "runId": "paused-run"}
    )
    return repo


async def test_resume_claim_skips_task_stopped_before_transaction(tmp_path):
    """任务在事务前停止时，恢复领取不能重新绑定原运行。"""
    repo = await _repo_with_paused_task(tmp_path)
    await repo.db.tasks.update_one({"id": "paused-task"}, {"$set": {"desiredState": "STOPPED"}})

    await schedule_once(repo)

    task = await repo.get("tasks", "paused-task")
    assert task["desiredState"] == "STOPPED"
    assert task["nodeId"] is None
    assert task["status"] == "STOPPED"
    assert await repo.db.endpoint_locks.find_one({"taskId": "paused-task", "runId": "paused-run"}) is not None


async def test_resume_claim_cas_does_not_overwrite_successor_owner(tmp_path, monkeypatch):
    """事务重读发现后继已领取时，旧快照不能覆盖其任务归属或运行锁。"""
    repo = await _repo_with_paused_task(tmp_path)
    collection_type = type(repo.db.tasks)
    original_find = collection_type.find_one
    replaced = False

    async def successor_claims_before_transaction_read(self, query, *args, **kwargs):
        nonlocal replaced
        is_resume_read = self.name == "tasks" and query.get("id") == "paused-task" and query.get("status") == "PAUSED"
        if is_resume_read and not replaced:
            replaced = True
            await self.update_one(
                {"id": "paused-task"},
                {"$set": {"nodeId": "node-b", "status": "PENDING", "desiredState": "RUNNING"}},
            )
        return await original_find(self, query, *args, **kwargs)

    monkeypatch.setattr(collection_type, "find_one", successor_claims_before_transaction_read)

    await schedule_once(repo)

    lock = await repo.db.endpoint_locks.find_one({"endpoint": "127.0.0.1:22"})
    task = await repo.get("tasks", "paused-task")
    assert replaced
    assert task["nodeId"] == "node-b"
    assert task["status"] == "PENDING"
    assert lock is not None
    assert lock["taskId"] == "paused-task"
    assert lock["runId"] == "paused-run"


async def test_resume_claim_cas_failure_returns_without_touching_existing_lock(tmp_path, monkeypatch):
    """任务 CAS 未命中时，领取返回失败且不删除暂停运行锁。"""
    repo = await _repo_with_paused_task(tmp_path)
    collection_type = type(repo.db.tasks)
    original_claim = collection_type.find_one_and_update
    rejected = False

    async def reject_task_cas(self, query, update, **kwargs):
        nonlocal rejected
        is_claim = self.name == "tasks" and query.get("id") == "paused-task" and query.get("status") == "PAUSED"
        if is_claim and not rejected:
            rejected = True
            return None
        return await original_claim(self, query, update, **kwargs)

    monkeypatch.setattr(collection_type, "find_one_and_update", reject_task_cas)

    await schedule_once(repo)

    lock = await repo.db.endpoint_locks.find_one({"endpoint": "127.0.0.1:22"})
    task = await repo.get("tasks", "paused-task")
    assert rejected
    assert lock is not None
    assert lock["runId"] == "paused-run"
    assert task["nodeId"] is None
    assert task["status"] == "PAUSED"


async def test_expired_resume_claim_is_recovered_after_scheduler_crash(tmp_path):
    """崩溃遗留的恢复租约到期后，后续调度周期可以继续同一运行。"""
    repo = await _repo_with_paused_task(tmp_path)
    await repo.db.tasks.update_one(
        {"id": "paused-task"},
        {"$set": {"resumeClaimToken": "abandoned", "resumeClaimExpires": now() - timedelta(seconds=1)}},
    )
    await repo.db.endpoint_locks.update_one(
        {"endpoint": "127.0.0.1:22"}, {"$set": {"claimToken": "abandoned"}}
    )

    await schedule_once(repo)

    task = await repo.get("tasks", "paused-task")
    lock = await repo.db.endpoint_locks.find_one({"endpoint": "127.0.0.1:22"})
    assert task["nodeId"] == "node-a"
    assert task["status"] == "PENDING"
    assert "resumeClaimToken" not in task
    assert lock is not None
    assert "claimToken" not in lock


async def test_scheduler_assigns_same_endpoint_to_separate_tasks(tmp_path):
    """同一设备端点的不同任务各自持有运行锁，不能因端点重复被阻塞。"""
    repo = Repository(
        AsyncMongoMockClient().db,
        Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path),
    )
    await repo.initialize()
    await repo.db.nodes.insert_one(
        {"id": "node-a", "heartbeat": now(), "diskPercent": 10, "accepting": True, "capacity": 100}
    )
    await repo.db.tasks.insert_many([
        {"id": "first", "ip": "127.0.0.1", "port": 22, "nodeId": None,
         "status": "PENDING", "desiredState": "RUNNING"},
        {"id": "second", "ip": "127.0.0.1", "port": 22, "nodeId": None,
         "status": "PENDING", "desiredState": "RUNNING"},
    ])

    await schedule_once(repo)

    assert (await repo.get("tasks", "first"))["nodeId"] == "node-a"
    assert (await repo.get("tasks", "second"))["nodeId"] == "node-a"
    assert await repo.db.endpoint_locks.count_documents({"endpoint": "127.0.0.1:22"}) == 2


async def test_task_lock_conflict_is_not_recovered_as_a_legacy_endpoint_conflict(tmp_path):
    """同任务已有运行锁仍保持阻塞，初始化迁移不能把它误恢复为待领取。"""
    repo = Repository(
        AsyncMongoMockClient().db,
        Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path),
    )
    await repo.initialize()
    await repo.db.nodes.insert_one(
        {"id": "node-a", "heartbeat": now(), "diskPercent": 10, "accepting": True, "capacity": 100}
    )
    await repo.db.tasks.insert_one(
        {"id": "task", "ip": "127.0.0.1", "port": 22, "nodeId": None,
         "status": "PENDING", "desiredState": "RUNNING"}
    )
    await repo.db.endpoint_locks.insert_one(
        {"endpoint": "127.0.0.1:22", "taskId": "task", "runId": "active-run"}
    )

    await schedule_once(repo)
    await repo.initialize()

    task = await repo.get("tasks", "task")
    assert task["status"] == "BLOCKED"
    assert task["error"] == "同一任务已有活动运行锁"
