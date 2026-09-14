"""SSH 暂停与继续回归：连接释放后保留运行身份、端点锁和定时命令预算。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.node.worker import Worker
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

pytestmark = pytest.mark.usefixtures("mock_claim_transaction")


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
    event = await repo.db.events.find_one({"taskId": "task", "type": "USER_PAUSED"})
    assert event["nodeId"] == repo.settings.node_id
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
    event = await repo.db.events.find_one({"taskId": "pending", "type": "USER_PAUSED"})
    assert event["nodeId"] == repo.settings.node_id


async def test_pause_stops_real_runtime_without_reconnect_and_preserves_run_lock_and_budget(tmp_path):
    """暂停实际连接后禁止重连；同一运行、端点锁和已占预算保留给后续恢复。"""
    class Connection:
        def __init__(self):
            self.received = asyncio.Queue()
            self.sent = []
            self.close_count = 0
            self.opened = asyncio.Event()

        async def read(self, _size=65536):
            self.opened.set()
            value = await self.received.get()
            return value or b""

        async def write(self, data):
            self.sent.append(data)

        async def close(self):
            self.close_count += 1
            self.received.put_nowait(None)

    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                        node_id="pause-node", start_background=False)
    repo = Repository(AsyncMongoMockClient().camera_logs, settings)
    await repo.initialize()
    task = {
        "id": "real-pause", "runId": "preserved-run", "nodeId": "pause-node", "generation": 1,
        "status": "PENDING", "desiredState": "RUNNING", "protocol": "SSH", "ip": "192.0.2.45",
        "port": 22, "passwordEncrypted": repo.encrypt(""), "storageIdentity": "pausedevice",
        "initialCommands": [{"command": "initialise"}], "scheduledCommands": [],
    }
    await repo.db.tasks.insert_one(task.copy())
    await repo.db.endpoint_locks.insert_one({"taskId": task["id"], "runId": task["runId"], "endpoint": "192.0.2.45:22"})
    await repo.db.budgets.insert_one({"_id": "preserved-run:periodic", "attempts": 2})
    connection = Connection()
    factory = AsyncMock(return_value=connection)
    runtime = SessionRuntime(repo, task, connection_factory=factory)
    await asyncio.wait_for(connection.opened.wait(), timeout=1)
    worker = Worker(repo)
    worker.active[task["id"]] = runtime
    await repo.db.tasks.update_one({"id": task["id"]}, {"$set": {"desiredState": "PAUSED"}})

    await worker.pause(runtime)

    paused = await repo.get("tasks", task["id"])
    assert factory.await_count == 1 and connection.close_count == 1
    assert runtime.background.done() and runtime.collector._closed.is_set()
    assert connection.sent == [b"initialise\n"]
    assert paused["status"] == "PAUSED" and paused["nodeId"] is None and paused["runId"] == "preserved-run"
    assert await repo.db.endpoint_locks.find_one({"taskId": task["id"], "runId": "preserved-run"}) is not None
    assert (await repo.db.budgets.find_one({"_id": "preserved-run:periodic"}))["attempts"] == 2
