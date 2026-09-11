"""隔离后保留运行与控制意图；本文件验证状态语义，事务原子性用真实副本集验证。"""

from datetime import timedelta

import pytest
from camera_logs.administration.isolation import confirm_node_isolation
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient


@pytest.fixture
def repo(tmp_path, monkeypatch, mock_claim_transaction):
    """Mongo 替身不支持事务；仅在测试中显式替换事务执行器。"""
    async def mock_transaction(_repo, callback):
        return await callback(None)
    monkeypatch.setattr("camera_logs.administration.isolation.isolation_transaction", mock_transaction)
    return Repository(AsyncMongoMockClient().db, Settings(log_root=tmp_path,
                      encryption_key=Fernet.generate_key().decode()))


@pytest.mark.parametrize("desired", ["RUNNING", "PAUSED", "STOPPED"])
async def test_isolation_preserves_desired_state_run_and_budget(repo, desired):
    await repo.initialize()
    await repo.db.nodes.insert_one({"id": "old", "heartbeat": now() - timedelta(seconds=31), "accepting": True})
    await repo.db.tasks.insert_one({"id": "task", "runId": "run", "sessionId": "session", "generation": 2,
        "nodeId": "old", "status": "BLOCKED", "desiredState": desired, "ip": "127.0.0.1", "port": 22})
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "run", "endpoint": "127.0.0.1:22"})
    await repo.db.runs.insert_one({"id": "run", "taskId": "task"})
    await repo.db.budgets.insert_one({"_id": "run:command", "attempts": 3})
    await repo.db.commands.insert_many([{"id": state, "taskId": "task", "runId": "run", "status": state}
                                       for state in ["SENDING", "QUEUED", "SENT"]])
    await confirm_node_isolation(repo, "old", "admin", "external isolation verified")
    task = await repo.db.tasks.find_one({"id": "task"})
    assert task["nodeId"] is None and task["runId"] == "run"
    assert task["desiredState"] == desired
    assert task["status"] == ("STOPPED" if desired == "STOPPED" else "PAUSED")
    assert (await repo.db.budgets.find_one({}))["attempts"] == 3
    assert bool(await repo.db.endpoint_locks.find_one({})) == (desired != "STOPPED")
    assert (await repo.db.commands.find_one({"id": "SENDING"}))["status"] == "UNKNOWN"
    assert (await repo.db.commands.find_one({"id": "QUEUED"}))["status"] == "CANCELLED"
    assert (await repo.db.commands.find_one({"id": "SENT"}))["status"] == "SENT"
    if desired == "RUNNING":
        await repo.db.nodes.insert_one({"id": "new", "heartbeat": now(), "accepting": True, "diskPercent": 10})
        await schedule_once(repo)
        resumed = await repo.db.tasks.find_one({"id": "task"})
        assert resumed["runId"] == "run" and resumed["nodeId"] == "new"
        assert resumed["generation"] == 3


async def test_live_node_confirmation_is_rejected_without_mutation(repo):
    await repo.db.nodes.insert_one({"id": "live", "heartbeat": now(), "accepting": True})
    claim = {"taskId": "live-task", "runId": "run", "generation": 1, "nodeId": "live", "token": "live"}
    await repo.db.ssh_connection_slots.insert_one({"_id": "192.0.2.35", "claims": [claim]})
    with pytest.raises(HTTPException) as error:
        await confirm_node_isolation(repo, "live", "admin", "evidence")
    assert error.value.status_code == 409
    assert (await repo.db.nodes.find_one({"id": "live"}))["accepting"] is True
    assert (await repo.db.ssh_connection_slots.find_one({"_id": "192.0.2.35"}))["claims"] == [claim]
    assert await repo.db.audit.count_documents({}) == 0


@pytest.mark.parametrize("desired", ["RUNNING", "PAUSED", "STOPPED"])
async def test_node_isolation_releases_only_confirmed_old_ssh_generation(repo, desired):
    """整节点隔离确认后归还旧SSH名额，其他任务及后继代次必须保留。"""
    await repo.initialize()
    await repo.db.nodes.insert_one({"id": "old", "heartbeat": now() - timedelta(seconds=31)})
    task = {"id": "task", "runId": "run", "generation": 2, "nodeId": "old", "protocol": "SSH",
            "status": "BLOCKED", "desiredState": desired, "ip": "192.0.2.35", "port": 22}
    await repo.db.tasks.insert_one(task)
    old_claim = {"taskId": "task", "runId": "run", "generation": 2, "nodeId": "old", "token": "old"}
    protected = [old_claim | {"taskId": "other", "token": "other"},
                 old_claim | {"generation": 3, "nodeId": "new", "token": "new"}]
    await repo.db.ssh_connection_slots.insert_one({"_id": task["ip"], "claims": [old_claim, *protected]})

    await confirm_node_isolation(repo, "old", "admin", "old worker terminated and device access isolated")

    slot = await repo.db.ssh_connection_slots.find_one({"_id": task["ip"]})
    assert slot["claims"] == protected
    current = await repo.db.tasks.find_one({"id": "task"})
    assert current["nodeId"] is None and current["desiredState"] == desired


@pytest.mark.parametrize("deleted", [False, True])
async def test_isolation_completes_configuration_restart_unless_resource_deleted(repo, deleted):
    """配置编辑的重启意图在隔离后继续执行，资源删除禁止重新启动。"""
    await repo.db.nodes.insert_one({"id": "old", "heartbeat": now() - timedelta(seconds=31)})
    await repo.db.tasks.insert_one({"id": "task", "runId": "run", "generation": 2,
        "nodeId": "old", "status": "BLOCKED", "desiredState": "STOPPED",
        "restartRequested": True, "resourceDeleted": deleted})
    await repo.db.runs.insert_one({"id": "run", "taskId": "task"})
    await confirm_node_isolation(repo, "old", "admin", "external isolation verified")
    task = await repo.db.tasks.find_one({"id": "task"})
    assert task["status"] == "STOPPED" and task["nodeId"] is None
    assert task["desiredState"] == ("STOPPED" if deleted else "RUNNING")
    if not deleted:
        assert task["restartRequested"] is False
    assert (await repo.db.runs.find_one({"id": "run"}))["endedAt"]
