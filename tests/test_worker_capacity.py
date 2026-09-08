"""模拟调度后磁盘恶化，验证节点在建连前重新检查本机容量。"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.worker import Worker
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


@pytest.mark.parametrize("percent", [90, 95, 100])
async def test_pending_task_waits_for_disk_recovery_before_connecting(tmp_path, percent):
    repo = Repository(AsyncMongoMockClient().db, Settings(
        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))
    await repo.initialize()
    await repo.db.tasks.insert_one({"id": "pending", "runId": "run", "nodeId": repo.settings.node_id,
                                    "status": "PENDING", "desiredState": "RUNNING"})
    worker = Worker(repo)
    worker.last_maintenance = time.monotonic()
    with patch("camera_logs.node.worker.shutil.disk_usage", return_value=SimpleNamespace(
            used=percent, total=100, free=100-percent)), patch("camera_logs.node.worker.SessionRuntime") as factory:
        await worker.tick()
        factory.assert_not_called()
        assert not worker.active
        task = await repo.get("tasks", "pending")
        assert task["status"] == "PENDING" and task["runId"] == "run"
        assert not (await repo.db.nodes.find_one({}))["accepting"]
    with patch("camera_logs.node.worker.shutil.disk_usage", return_value=SimpleNamespace(
            used=89, total=100, free=11)), patch("camera_logs.node.worker.SessionRuntime") as factory:
        await worker.tick()
        factory.assert_called_once()
        assert "pending" in worker.active


async def test_disk_alerts_record_transitions_without_repeating_each_tick(tmp_path):
    repo = Repository(AsyncMongoMockClient().db, Settings(
        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))
    await repo.initialize()
    worker = Worker(repo)
    worker.last_maintenance = time.monotonic()
    for percent in [79, 80, 81, 90, 91, 95, 96, 79]:
        with patch("camera_logs.node.worker.shutil.disk_usage", return_value=SimpleNamespace(
                used=percent, total=100, free=100-percent)):
            await worker.tick()
    events = [event async for event in repo.db.events.find({"type": "DISK_PRESSURE_CHANGED"})]
    assert [event["level"] for event in events] == ["WARNING", "NO_ADMISSION", "CRITICAL", "NORMAL"]


@pytest.mark.parametrize("restart", [False, True])
async def test_waiting_task_can_stop_without_opening_a_connection(tmp_path, restart):
    repo = Repository(AsyncMongoMockClient().db, Settings(
        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))
    await repo.initialize()
    await repo.db.tasks.insert_one({"id": "pending", "runId": "run", "nodeId": repo.settings.node_id,
                                    "status": "PENDING", "desiredState": "STOPPED", "restartRequested": restart})
    await repo.db.runs.insert_one({"id": "run"})
    await repo.db.endpoint_locks.insert_one({"taskId": "pending", "runId": "run", "endpoint": "127.0.0.1:22"})
    for identifier, desired in [("start", "RUNNING"), ("stop", "STOPPED")]:
        await repo.db.operations.insert_one({"id": identifier, "taskId": "pending",
                                             "desiredState": desired, "status": "PENDING"})
    worker = Worker(repo)
    worker.last_maintenance = time.monotonic()
    with patch("camera_logs.node.worker.shutil.disk_usage", return_value=SimpleNamespace(
            used=95, total=100, free=5)), patch("camera_logs.node.worker.SessionRuntime") as factory:
        await worker.tick()
        factory.assert_not_called()
    task = await repo.get("tasks", "pending")
    assert task["status"] == "STOPPED" and task["nodeId"] is None
    assert task["desiredState"] == ("RUNNING" if restart else "STOPPED")
    assert task["restartRequested"] is False
    assert not await repo.db.endpoint_locks.count_documents({})
    assert (await repo.get("runs", "run"))["endedAt"]
    assert (await repo.get("operations", "start"))["status"] == "FAILED"
    assert (await repo.get("operations", "stop"))["status"] == "SUCCEEDED"


@pytest.mark.parametrize("percent,stopped", [(90, False), (95, True)])
async def test_existing_collection_only_stops_at_critical_disk_pressure(tmp_path, percent, stopped):
    repo = Repository(AsyncMongoMockClient().db, Settings(
        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))
    await repo.initialize()
    task = {"id": "active", "runId": "run", "nodeId": repo.settings.node_id,
            "status": "COLLECTING", "desiredState": "RUNNING"}
    await repo.db.tasks.insert_one(task)
    runtime = SimpleNamespace(task=task, input_bytes=0, stopping=False, error=None,
                              background=asyncio.get_running_loop().create_future(),
                              background_failure=lambda: None, stop=AsyncMock())
    worker = Worker(repo)
    worker.active[task["id"]] = runtime
    worker.last_maintenance = time.monotonic()
    with patch("camera_logs.node.worker.shutil.disk_usage", return_value=SimpleNamespace(
            used=percent, total=100, free=100-percent)):
        await worker.tick()
        await asyncio.gather(*worker.releases.values())
    assert runtime.stop.await_count == int(stopped)
    result = await repo.get("tasks", "active")
    if stopped:
        assert result["status"] == "ERROR" and result["desiredState"] == "STOPPED"
        assert "磁盘" in result["error"]
    else:
        assert result["status"] == "COLLECTING" and task["id"] in worker.active
