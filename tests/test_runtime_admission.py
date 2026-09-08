"""运行时建连准入：过期领取与阻塞状态不能继续重连或覆盖隔离状态。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncssh
import pytest
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


def repository(tmp_path):
    """提供不读取环境文件的独立测试仓储。"""
    return Repository(AsyncMongoMockClient().db, Settings(
        _env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
    ))


@pytest.mark.parametrize("change", [{"generation": 2}, {"nodeId": "other"}, {"runId": "new"}, {"status": "BLOCKED"}])
async def test_obsolete_assignment_does_not_open_connection(tmp_path, change):
    """即使旧运行对象仍存活，数据库准入失败也必须在调用连接工厂前退出。"""
    repo = repository(tmp_path)
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1,
            "status": "PENDING", "passwordEncrypted": repo.encrypt(""), "scheduledCommands": []}
    await repo.db.tasks.insert_one(task | change)
    factory = AsyncMock(side_effect=asyncssh.PermissionDenied("synthetic denied"))
    runtime = SessionRuntime(repo, task, factory)
    await asyncio.wait_for(runtime.background, 1)
    factory.assert_not_awaited()
    assert runtime.stopping and runtime.error is None


async def test_blocking_during_connecting_callback_prevents_factory(tmp_path, monkeypatch):
    """复位已成功但 CONNECTING 回调前刚被阻塞，仍不能继续打开连接。"""
    repo = repository(tmp_path)
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1,
            "status": "PENDING", "passwordEncrypted": repo.encrypt(""), "scheduledCommands": []}
    await repo.db.tasks.insert_one(task.copy())
    original = SessionRuntime.on_state

    async def block_then_callback(self, state, details):
        if state == "CONNECTING":
            await repo.db.tasks.update_one({"id": "task"}, {"$set": {"status": "BLOCKED"}})
        await original(self, state, details)

    monkeypatch.setattr(SessionRuntime, "on_state", block_then_callback)
    factory = AsyncMock(side_effect=asyncssh.PermissionDenied("synthetic denied"))
    runtime = SessionRuntime(repo, task, factory)
    await asyncio.wait_for(runtime.background, 1)
    factory.assert_not_awaited()
    assert (await repo.db.tasks.find_one({"id": "task"}))["status"] == "BLOCKED"


@pytest.mark.parametrize("state", ["CLOSED", "RECONNECTING", "COLLECTING"])
async def test_blocked_task_is_not_reopened_by_state_callback(tmp_path, state):
    """相同代次的隔离状态也不能被关闭及重连回调恢复为正常采集。"""
    repo = repository(tmp_path)
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1, "status": "BLOCKED"}
    await repo.db.tasks.insert_one(task.copy())
    runtime = object.__new__(SessionRuntime)
    runtime.task, runtime.repo, runtime.stopping = task, repo, True
    runtime.collector = SimpleNamespace(session_id="session")
    await runtime.on_state(state, {"sessionId": "session"})
    assert (await repo.db.tasks.find_one({"id": "task"}))["status"] == "BLOCKED"
