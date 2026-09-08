"""调度取消不能在任务、运行锁和运行记录之间留下不一致状态。"""

import asyncio

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

pytestmark = pytest.mark.usefixtures("mock_claim_transaction")


async def _repository_with_pending_task(tmp_path):
    """建立一个可立即领取的任务和节点，全部使用内存 MongoMock。"""
    repo = Repository(
        AsyncMongoMockClient().db,
        Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path),
    )
    await repo.initialize()
    await repo.db.nodes.insert_one({
        "id": "node-a",
        "heartbeat": now(),
        "diskPercent": 10,
        "accepting": True,
        "capacity": 1,
        "writeLatencyMs": 0,
    })
    await repo.db.tasks.insert_one({
        "id": "pending-task",
        "ip": "127.0.0.1",
        "port": 2023,
        "nodeId": None,
        "status": "PENDING",
        "desiredState": "RUNNING",
    })
    return repo


async def _rollback_claim_transaction(repo, callback):
    """以快照恢复 MongoMock 取消前状态，仅验证事务回调边界而非真实原子性。"""
    names = ("tasks", "endpoint_locks", "runs")
    snapshot = {name: [item async for item in repo.db[name].find({})] for name in names}
    try:
        return await callback(None)
    except BaseException:
        for name in names:
            collection = repo.db[name]
            await collection.delete_many({})
            if snapshot[name]:
                await collection.insert_many(snapshot[name])
        raise


async def test_scheduler_cancellation_after_lock_insert_allows_next_cycle_claim(tmp_path, monkeypatch):
    """领取回调取消会传播；快照替身恢复后下一周期可重新领取。"""
    repo = await _repository_with_pending_task(tmp_path)
    monkeypatch.setattr("camera_logs.tasks.claim.claim_transaction", _rollback_claim_transaction)
    collection_type = type(repo.db.tasks)
    original_claim = collection_type.find_one_and_update
    cancelled = False

    async def cancel_before_task_claim(self, query, update, **kwargs):
        """在普通任务 CAS 前取消，触发测试替身恢复先前的锁写入。"""
        nonlocal cancelled
        is_task_claim = (
            self.name == "tasks"
            and query.get("id") == "pending-task"
            and query.get("nodeId") is None
            and query.get("desiredState") == "RUNNING"
        )
        if is_task_claim and not cancelled:
            cancelled = True
            raise asyncio.CancelledError()
        return await original_claim(self, query, update, **kwargs)

    monkeypatch.setattr(collection_type, "find_one_and_update", cancel_before_task_claim)
    with pytest.raises(asyncio.CancelledError):
        await schedule_once(repo)
    monkeypatch.setattr(collection_type, "find_one_and_update", original_claim)

    await schedule_once(repo)

    task = await repo.get("tasks", "pending-task")
    lock = await repo.db.endpoint_locks.find_one({"taskId": "pending-task"})
    run = await repo.db.runs.find_one({"id": task.get("runId")})
    assert cancelled
    assert task["nodeId"] == "node-a"
    assert lock is not None and lock["runId"] == task["runId"]
    assert run is not None and run["nodeId"] == task["nodeId"]


async def test_scheduler_cancellation_after_task_claim_keeps_run_state_consistent(tmp_path, monkeypatch):
    """运行记录写入前取消会传播；快照替身恢复后下一周期保持三项一致。"""
    repo = await _repository_with_pending_task(tmp_path)
    monkeypatch.setattr("camera_logs.tasks.claim.claim_transaction", _rollback_claim_transaction)
    collection_type = type(repo.db.runs)
    original_update = collection_type.update_one
    cancelled = False

    async def cancel_before_run_write(self, query, update, **kwargs):
        """首次写入运行记录前取消，模拟 CAS 和运行记录之间的调度中断。"""
        nonlocal cancelled
        if self.name == "runs" and query.get("id") and not cancelled:
            cancelled = True
            raise asyncio.CancelledError()
        return await original_update(self, query, update, **kwargs)

    monkeypatch.setattr(collection_type, "update_one", cancel_before_run_write)
    with pytest.raises(asyncio.CancelledError):
        await schedule_once(repo)
    monkeypatch.setattr(collection_type, "update_one", original_update)

    await schedule_once(repo)

    task = await repo.get("tasks", "pending-task")
    lock = await repo.db.endpoint_locks.find_one({"taskId": "pending-task"})
    run = await repo.db.runs.find_one({"id": task.get("runId")})
    assert cancelled
    assert task["nodeId"] == "node-a"
    assert lock is not None and lock["runId"] == task["runId"]
    assert run is not None and run["nodeId"] == task["nodeId"]
