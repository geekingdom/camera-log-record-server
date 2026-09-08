"""调度领取竞态：恢复锁只在确认任务已停止后才能回收。"""

from datetime import timedelta

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


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


async def test_resume_claim_failure_after_stop_releases_stale_paused_lock(tmp_path, monkeypatch):
    """停止在恢复锁确认后发生时，旧运行锁不得阻塞下一次启动。"""
    repo = await _repo_with_paused_task(tmp_path)
    collection_type = type(repo.db.tasks)
    original_claim = collection_type.find_one_and_update

    async def stop_before_claim(self, query, update, **kwargs):
        token = query.get("resumeClaimToken")
        if self.name != "tasks" or not isinstance(token, str):
            return await original_claim(self, query, update, **kwargs)
        await self.update_one(
            {"id": "paused-task"},
            {"$set": {"desiredState": "STOPPED"}},
        )
        return None

    monkeypatch.setattr(collection_type, "find_one_and_update", stop_before_claim)

    await schedule_once(repo)

    task = await repo.get("tasks", "paused-task")
    assert task["desiredState"] == "STOPPED"
    assert task["nodeId"] is None
    assert await repo.db.endpoint_locks.find_one({"endpoint": "127.0.0.1:22"}) is None


async def test_resume_claim_failure_keeps_lock_when_another_owner_claimed_task(tmp_path, monkeypatch):
    """并发领取已写入新归属时，失败恢复者不能删除该运行仍在使用的锁。"""
    repo = await _repo_with_paused_task(tmp_path)
    collection_type = type(repo.db.tasks)
    original_claim = collection_type.find_one_and_update

    async def claim_elsewhere_before_claim(self, query, update, **kwargs):
        token = query.get("resumeClaimToken")
        if self.name != "tasks" or not isinstance(token, str):
            return await original_claim(self, query, update, **kwargs)
        await self.update_one(
            {"id": "paused-task"},
            {"$set": {"nodeId": "node-b", "status": "PENDING", "desiredState": "RUNNING"}},
        )
        return None

    monkeypatch.setattr(collection_type, "find_one_and_update", claim_elsewhere_before_claim)

    await schedule_once(repo)

    lock = await repo.db.endpoint_locks.find_one({"endpoint": "127.0.0.1:22"})
    assert lock is not None
    assert lock["taskId"] == "paused-task"
    assert lock["runId"] == "paused-run"


async def test_stale_resume_cleanup_cannot_delete_successor_claim_token(tmp_path, monkeypatch):
    """旧恢复补偿读到停止状态后，后继 token 的锁必须保留。"""
    repo = await _repo_with_paused_task(tmp_path)
    collection_type = type(repo.db.tasks)
    original_claim = collection_type.find_one_and_update
    original_find = collection_type.find_one

    async def stop_before_claim(self, query, update, **kwargs):
        token = query.get("resumeClaimToken")
        if self.name != "tasks" or not isinstance(token, str):
            return await original_claim(self, query, update, **kwargs)
        await self.update_one({"id": "paused-task"}, {"$set": {"desiredState": "STOPPED"}})
        return None

    async def successor_claims_after_stop_read(self, query, *args, **kwargs):
        stopped = await original_find(self, query, *args, **kwargs)
        token = query.get("resumeClaimToken")
        if self.name == "tasks" and stopped and isinstance(token, str):
            successor_token = "successor-token"
            await self.update_one(
                {"id": "paused-task"},
                {"$set": {"nodeId": "node-b", "status": "PENDING", "desiredState": "RUNNING",
                          "resumeClaimToken": successor_token,
                          "resumeClaimExpires": now() + timedelta(seconds=10)}},
            )
            await repo.db.endpoint_locks.update_one(
                {"endpoint": "127.0.0.1:22"}, {"$set": {"claimToken": successor_token}}
            )
        return stopped

    monkeypatch.setattr(collection_type, "find_one_and_update", stop_before_claim)
    monkeypatch.setattr(collection_type, "find_one", successor_claims_after_stop_read)

    await schedule_once(repo)

    lock = await repo.db.endpoint_locks.find_one({"endpoint": "127.0.0.1:22"})
    task = await repo.get("tasks", "paused-task")
    assert lock is not None
    assert lock["claimToken"] == "successor-token"
    assert task["resumeClaimToken"] == "successor-token"


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
