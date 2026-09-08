"""SSH 暂停与继续回归：连接释放后保留运行身份、端点锁和定时命令预算。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.node.worker import Worker
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def test_pause_releases_connection_but_resume_preserves_run_and_budget(tmp_path):
    repo = Repository(AsyncMongoMockClient().db, Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))
    await repo.initialize()
    task = {"id": "task", "protocol": "SSH", "ip": "127.0.0.1", "port": 22, "runId": "run",
            "nodeId": "node", "status": "COLLECTING", "desiredState": "PAUSED", "generation": 1}
    await repo.db.tasks.insert_one(task)
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "run", "endpoint": "127.0.0.1:22"})
    await repo.db.budgets.insert_one({"_id": "run:command", "attempts": 2})
    runtime = SimpleNamespace(task=task, stop=AsyncMock())
    worker = Worker(repo)
    worker.active["task"] = runtime
    await worker.pause(runtime)
    runtime.stop.assert_awaited_once()
    assert (await repo.get("tasks", "task"))["status"] == "PAUSED"
    assert await repo.db.endpoint_locks.count_documents({}) == 1
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {"desiredState": "RUNNING"}})
    await schedule_once(repo)
    assert (await repo.get("tasks", "task"))["status"] == "PAUSED"
    await repo.db.nodes.insert_one({"id": "node", "heartbeat": now(), "diskPercent": 10, "accepting": True, "capacity": 100})
    await schedule_once(repo)
    resumed = await repo.get("tasks", "task")
    assert resumed["runId"] == "run" and resumed["status"] == "PENDING"
    assert (await repo.db.budgets.find_one({"_id": "run:command"}))["attempts"] == 2


async def test_pause_pending_assignment_preserves_lock_without_marking_blocked(tmp_path):
    repo = Repository(AsyncMongoMockClient().db, Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))
    await repo.initialize()
    task = {"id": "pending", "runId": "run", "nodeId": "node", "status": "PENDING", "desiredState": "PAUSED"}
    await repo.db.tasks.insert_one(task)
    await repo.db.endpoint_locks.insert_one({"taskId": "pending", "runId": "run", "endpoint": "127.0.0.1:22"})
    await repo.db.operations.insert_one({"id": "pause", "taskId": "pending", "desiredState": "PAUSED", "status": "PENDING"})
    worker = Worker(repo)
    await worker.pause_pending(task)
    paused = await repo.get("tasks", "pending")
    assert paused["status"] == "PAUSED" and paused["nodeId"] is None
    assert await repo.db.endpoint_locks.count_documents({}) == 1
    assert (await repo.get("operations", "pause"))["status"] == "SUCCEEDED"
