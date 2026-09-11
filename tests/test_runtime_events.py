"""验证采集读取异常和空闲超时的事件可追踪性及退役回调隔离。"""

import asyncio
from types import SimpleNamespace

import pytest
from camera_logs.collection import runtime_events
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def _runtime():
    repo = Repository(AsyncMongoMockClient().db, Settings(_env_file=None, encryption_key=Fernet.generate_key().decode()))
    await repo.initialize()
    task = {"id": "task", "runId": "run", "generation": 3, "nodeId": "node", "status": "COLLECTING",
            "desiredState": "RUNNING", "ip": "192.0.2.9"}
    await repo.db.tasks.insert_one(task)
    runtime = object.__new__(SessionRuntime)
    runtime.repo, runtime.task = repo, task
    runtime.retired, runtime.stopping = False, False
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("state,message", [
    ("READ_ERROR", "设备连接读取异常，正在重新连接"),
    ("IDLE_TIMEOUT", "未收到设备日志达到空闲超时阈值，准备重新连接"),
])
async def test_reconnect_causes_are_persisted_before_mapping_to_reconnecting(state, message):
    runtime = await _runtime()
    await runtime.on_state(state, {"sessionId": "session", "error": "device body must not persist"})
    event = await runtime.repo.db.events.find_one({"taskId": "task", "type": state})
    assert event["runId"] == "run" and event["sessionId"] == "session"
    assert event["nodeId"] == runtime.repo.settings.node_id
    assert event["message"] == message
    assert (event["level"], event["outcome"]) == ("WARNING", "UNKNOWN")
    assert "error" not in event
    assert (await runtime.repo.get("tasks", "task"))["status"] == "RECONNECTING"


@pytest.mark.asyncio
async def test_retired_or_blocked_callback_cannot_write_event_or_overwrite_status():
    runtime = await _runtime()
    runtime.retired = True
    await runtime.on_state("READ_ERROR", {"sessionId": "old"})
    assert await runtime.repo.db.events.count_documents({}) == 0
    runtime.retired = False
    await runtime.repo.db.tasks.update_one({"id": "task"}, {"$set": {"status": "BLOCKED"}})
    await runtime.on_state("IDLE_TIMEOUT", {"sessionId": "old"})
    assert await runtime.repo.db.events.count_documents({}) == 0
    assert (await runtime.repo.get("tasks", "task"))["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_event_write_failure_does_not_prevent_reconnect_status():
    runtime = await _runtime()

    class FailingEvents:
        async def insert_one(self, _document):
            raise RuntimeError("event store unavailable")

    runtime.repo = SimpleNamespace(db=SimpleNamespace(tasks=runtime.repo.db.tasks,
                                                        operations=runtime.repo.db.operations,
                                                        events=FailingEvents()),
                                   settings=runtime.repo.settings)
    await runtime.on_state("READ_ERROR", {"sessionId": "session"})
    updated = await runtime.repo.db.tasks.find_one({"id": "task"})
    assert updated["status"] == "RECONNECTING"


@pytest.mark.asyncio
async def test_event_write_timeout_does_not_prevent_reconnect_status(monkeypatch):
    """运行事件观测卡住也不能让采集连接等待，任务仍立即进入重连状态。"""
    runtime = await _runtime()

    class BlockingEvents:
        async def insert_one(self, _document):
            await asyncio.Event().wait()

    monkeypatch.setattr(runtime_events, "_EVENT_WRITE_TIMEOUT_SECONDS", 0.01)
    runtime.repo = SimpleNamespace(db=SimpleNamespace(tasks=runtime.repo.db.tasks,
                                                        operations=runtime.repo.db.operations,
                                                        events=BlockingEvents()),
                                   settings=runtime.repo.settings)
    await runtime.on_state("IDLE_TIMEOUT", {"sessionId": "session"})
    updated = await runtime.repo.db.tasks.find_one({"id": "task"})
    assert updated["status"] == "RECONNECTING"


@pytest.mark.asyncio
async def test_late_session_callback_cannot_overwrite_current_session_or_write_event():
    """新 Collector 已创建后，旧会话读取异常不应改变新会话的采集状态。"""
    runtime = await _runtime()
    runtime.collector = SimpleNamespace(session_id="current-session")
    await runtime.on_state("READ_ERROR", {"sessionId": "retired-session"})
    assert await runtime.repo.db.events.count_documents({}) == 0
    task = await runtime.repo.get("tasks", "task")
    assert task["status"] == "COLLECTING" and task.get("sessionId") is None
