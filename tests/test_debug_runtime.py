"""PSH 握手失败的运行时收尾测试：领域错误可释放，存储错误仍需上报。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection.psh_dialogue import PshSwitchError
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.worker import Worker
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def repository(tmp_path):
    """建立带任务、运行和端点锁的最小节点仓储，覆盖 Worker.release 的持久化路径。"""
    settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node-debug")
    repo = Repository(AsyncMongoMockClient().db, settings)
    await repo.initialize()
    task = {
        "id": "debug-task", "runId": "debug-run", "protocol": "TELNET_SERIAL", "ip": "127.0.0.1", "port": 23,
        "passwordEncrypted": repo.encrypt(""), "scheduledCommands": [], "nodeId": "node-debug",
        "status": "COLLECTING", "desiredState": "RUNNING",
    }
    await repo.db.tasks.insert_one(task)
    await repo.db.runs.insert_one({"id": "debug-run"})
    await repo.db.endpoint_locks.insert_one({"taskId": "debug-task", "runId": "debug-run", "endpoint": "127.0.0.1:23"})
    await repo.db.operations.insert_one({"id": "stop", "taskId": "debug-task", "desiredState": "STOPPED", "status": "PENDING"})
    return repo, task


class HandshakeFailureCollector:
    """模拟连接已完成收尾后抛出的 PSH 领域错误，不能触发运行时重连。"""

    instances = 0

    def __init__(self, *_args, **_kwargs) -> None:
        type(self).instances += 1
        self.session_id = "failed-session"

    async def start(self) -> None:
        raise PshSwitchError("synthetic psh handshake failure")

    async def stop(self) -> None:
        raise PshSwitchError("synthetic psh handshake failure")


async def test_psh_failure_ends_runtime_and_worker_releases_lock_without_reconnect(tmp_path, monkeypatch):
    repo, task = await repository(tmp_path)
    monkeypatch.setattr("camera_logs.collection.runtime.Collector", HandshakeFailureCollector)
    HandshakeFailureCollector.instances = 0
    runtime = SessionRuntime(repo, task, connection_factory=AsyncMock())
    await runtime.background

    assert HandshakeFailureCollector.instances == 1
    assert runtime.error == "synthetic psh handshake failure"
    assert runtime.background_failure() is None

    worker = Worker(repo)
    worker.active[task["id"]] = runtime
    await worker.release(runtime)

    stored = await repo.get("tasks", task["id"])
    assert stored["status"] == "ERROR"
    assert stored["desiredState"] == "STOPPED"
    assert stored["nodeId"] is None
    assert await repo.db.endpoint_locks.count_documents({"taskId": task["id"]}) == 0
    assert (await repo.get("operations", "stop"))["status"] == "FAILED"
    assert task["id"] not in worker.active


async def test_storage_stop_error_is_not_absorbed_as_psh_failure(tmp_path):
    repo, task = await repository(tmp_path)
    runtime = SimpleNamespace(task=task, error=None, stop=AsyncMock(side_effect=OSError("synthetic storage failure")))
    worker = Worker(repo)

    with pytest.raises(OSError, match="synthetic storage failure"):
        await worker.release(runtime)

    # 未确认收尾时不得伪造端点已释放，也不得写成正常 STOPPED。
    stored = await repo.get("tasks", task["id"])
    assert stored["status"] == "COLLECTING"
    assert await repo.db.endpoint_locks.count_documents({"taskId": task["id"]}) == 1
