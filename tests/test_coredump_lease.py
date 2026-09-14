"""资源级 Coredump 租约协调器回归。"""

import logging
from datetime import UTC, timedelta
from types import SimpleNamespace

from camera_logs.collection.coredump_lease import record_coredump_status, release_coredump_lease
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def test_release_coredump_lease_keeps_successor_lease_and_releases_matching_owner(tmp_path):
    """释放条件必须包含任务、运行、代次和节点，避免旧会话缩短后继租约。"""
    repo = Repository(AsyncMongoMockClient().camera_logs, Settings(
        _env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node-a"))
    task = {"id": "task-a", "runId": "run-a", "generation": 3, "nodeId": "node-a", "resourceId": "resource-a"}
    successor_until = now() + timedelta(seconds=70)
    await repo.db.resources.insert_one({"id": "resource-a", "coredumpLeaseTaskId": "task-b",
                                        "coredumpLeaseRunId": "run-b", "coredumpLeaseGeneration": 4,
                                        "coredumpLeaseNodeId": "node-b", "coredumpLeaseUntil": successor_until})

    await release_coredump_lease(repo, task)
    stored = (await repo.db.resources.find_one({"id": "resource-a"}))["coredumpLeaseUntil"].replace(tzinfo=UTC)
    assert stored > now() + timedelta(seconds=60)

    await repo.db.resources.update_one({"id": "resource-a"}, {"$set": {
        "coredumpLeaseTaskId": task["id"], "coredumpLeaseRunId": task["runId"],
        "coredumpLeaseGeneration": task["generation"], "coredumpLeaseNodeId": task["nodeId"],
        "coredumpLeaseUntil": successor_until}})
    await release_coredump_lease(repo, task)
    released = (await repo.db.resources.find_one({"id": "resource-a"}))["coredumpLeaseUntil"].replace(tzinfo=UTC)
    assert released <= now()


async def test_coredump_mounted_event_deduplicates_by_session_but_keeps_status_fresh(tmp_path):
    """连续成功挂载不刷审计，故障恢复和新会话仍须形成独立事件。"""
    repo = Repository(AsyncMongoMockClient().camera_logs, Settings(
        _env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node-a"))
    task = {"id": "task-a", "runId": "run-a", "generation": 3, "nodeId": "node-a"}
    await repo.db.tasks.insert_one(task)
    collector = SimpleNamespace(session_id="session-a")

    await record_coredump_status(repo, task, collector, "MOUNTED", None, logging.getLogger(__name__))
    await record_coredump_status(repo, task, collector, "MOUNTED", None, logging.getLogger(__name__))
    stored = await repo.db.tasks.find_one({"id": task["id"]})
    assert stored["coredumpMountStatus"] == "MOUNTED" and stored["coredumpCheckedAt"] is not None
    assert await repo.db.events.count_documents({"taskId": task["id"], "status": "MOUNTED"}) == 1

    await record_coredump_status(repo, task, collector, "FAILED", "TimeoutError", logging.getLogger(__name__))
    await record_coredump_status(repo, task, collector, "MOUNTED", None, logging.getLogger(__name__))
    collector.session_id = "session-b"
    await record_coredump_status(repo, task, collector, "MOUNTED", None, logging.getLogger(__name__))

    events = [event async for event in repo.db.events.find({"taskId": task["id"]})]
    assert [(event["sessionId"], event["status"]) for event in events] == [
        ("session-a", "MOUNTED"), ("session-a", "FAILED"),
        ("session-a", "MOUNTED"), ("session-b", "MOUNTED"),
    ]


async def test_coredump_mounted_retries_after_event_insert_failure(tmp_path):
    """仅成功发布才建立去重签名，事件存储失败后不能吞掉下一次成功通知。"""
    repo = Repository(AsyncMongoMockClient().camera_logs, Settings(
        _env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node-a"))
    task = {"id": "task-a", "runId": "run-a", "generation": 3, "nodeId": "node-a"}
    await repo.db.tasks.insert_one(task)
    collector = SimpleNamespace(session_id="session-a")
    original_insert = repo.db.events.insert_one
    calls = 0

    async def insert_one(document):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("模拟事件存储失败")
        return await original_insert(document)

    failing_repo = SimpleNamespace(
        settings=repo.settings,
        db=SimpleNamespace(events=SimpleNamespace(insert_one=insert_one), tasks=repo.db.tasks),
    )
    await record_coredump_status(failing_repo, task, collector, "MOUNTED", None, logging.getLogger(__name__))
    await record_coredump_status(failing_repo, task, collector, "MOUNTED", None, logging.getLogger(__name__))

    assert calls == 2
    assert await repo.db.events.count_documents({"taskId": task["id"], "status": "MOUNTED"}) == 1
