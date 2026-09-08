"""监督周期隔离回归：失去归属必须停连，过期收尾失败不能污染后继状态。"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.worker import Worker
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def setup_worker(tmp_path, monkeypatch):
    """显式提供测试密钥与磁盘余量，隔离开发机配置及后台维护。"""
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(),
                        log_root=tmp_path, node_id="node")
    repo = Repository(AsyncMongoMockClient().db, settings)
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1,
            "status": "COLLECTING", "desiredState": "RUNNING"}
    await repo.db.tasks.insert_one(task.copy())
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "run", "endpoint": "host:22"})
    runtime = SimpleNamespace(task=task, input_bytes=0, stopping=False, error=None,
                              background=asyncio.get_running_loop().create_future(),
                              background_failure=lambda: None, stop=AsyncMock())
    worker = Worker(repo)
    worker.active["task"] = runtime
    worker.last_maintenance = time.monotonic()
    monkeypatch.setattr("camera_logs.node.worker.shutil.disk_usage",
                        lambda _: SimpleNamespace(used=10, total=100, free=90))
    return worker, repo, runtime


@pytest.mark.parametrize("change", ["missing", "node", "generation", "run", "blocked", "isolated"])
async def test_tick_closes_disowned_or_blocked_runtime_without_releasing_lock(tmp_path, monkeypatch, change):
    """隔离只证明本机连接关闭，不擅自放行任务锁或修改后继领取状态。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    if change == "missing":
        await repo.db.tasks.delete_one({"id": "task"})
    elif change == "isolated":
        await repo.db.nodes.insert_one({"id": "node", "isolated": True})
    else:
        changed = {"node": {"nodeId": "other"}, "generation": {"generation": 2},
                   "run": {"runId": "other"}, "blocked": {"status": "BLOCKED"}}[change]
        await repo.db.tasks.update_one({"id": "task"}, {"$set": changed})
    before = await repo.db.tasks.find_one({"id": "task"})

    await worker.tick()
    await asyncio.gather(*worker.releases.values())

    runtime.stop.assert_awaited_once()
    assert "task" not in worker.active
    assert await repo.db.tasks.find_one({"id": "task"}) == before
    assert await repo.db.endpoint_locks.count_documents({}) == 1


async def test_release_failure_after_reassignment_does_not_block_new_owner(tmp_path, monkeypatch):
    """关闭过程中发生重新领取，旧关闭错误不能失败新代次的操作。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {"desiredState": "STOPPED"}})
    started, finish = asyncio.Event(), asyncio.Event()

    async def failed_stop():
        started.set()
        await finish.wait()
        raise OSError("synthetic close failure")

    runtime.stop = AsyncMock(side_effect=failed_stop)
    await worker.tick()
    await asyncio.wait_for(started.wait(), 1)
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {
        "nodeId": "other", "generation": 2, "desiredState": "RUNNING", "status": "COLLECTING",
    }})
    await repo.db.operations.insert_one({"id": "new-start", "taskId": "task", "status": "PENDING"})
    before = await repo.db.tasks.find_one({"id": "task"})
    finish.set()
    await asyncio.gather(*worker.releases.values(), return_exceptions=True)
    await worker.tick()
    await asyncio.gather(*worker.releases.values(), return_exceptions=True)

    assert await repo.db.tasks.find_one({"id": "task"}) == before
    assert (await repo.db.operations.find_one({"id": "new-start"}))["status"] == "PENDING"
    assert worker.active["task"] is runtime


async def test_current_release_failure_blocks_only_current_owner(tmp_path, monkeypatch):
    """未发生重新领取的关闭失败仍需阻塞并保留实例，不得伪造连接已关闭。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    runtime.stop = AsyncMock(side_effect=OSError("synthetic close failure"))
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {"desiredState": "STOPPED"}})
    await repo.db.operations.insert_one({"id": "stop", "taskId": "task", "status": "PENDING"})

    await worker.tick()
    await asyncio.gather(*worker.releases.values(), return_exceptions=True)

    assert (await repo.db.tasks.find_one({"id": "task"}))["status"] == "BLOCKED"
    assert (await repo.db.operations.find_one({"id": "stop"}))["status"] == "FAILED"
    assert worker.active["task"] is runtime
    assert await repo.db.endpoint_locks.count_documents({}) == 1


async def test_failed_task_snapshot_does_not_mean_ownership_disappeared(tmp_path, monkeypatch):
    """任务查询失败立即传播至现有数据库失联处理，不能按空列表隔离所有连接。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    collection_type = type(repo.db.tasks)
    original = collection_type.find

    def fail_task_query(self, *args, **kwargs):
        if self.name == "tasks":
            raise OSError("synthetic task query failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(collection_type, "find", fail_task_query)
    with pytest.raises(OSError, match="synthetic task query failure"):
        await worker.tick()

    runtime.stop.assert_not_awaited()
    assert worker.active["task"] is runtime and not worker.releases


@pytest.mark.parametrize("superseded", [False, True])
async def test_shutdown_failure_is_scoped_to_original_owner(tmp_path, monkeypatch, superseded):
    """进程退出期间关闭失败也必须使用旧归属，不能遗漏告警或污染后继。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    await repo.db.operations.insert_one({"id": "operation", "taskId": "task", "status": "PENDING"})

    async def failing_close():
        if superseded:
            await repo.db.tasks.update_one({"id": "task"}, {"$set": {
                "generation": 2, "status": "COLLECTING", "desiredState": "RUNNING",
            }})
        raise OSError("synthetic shutdown failure")

    runtime.stop = AsyncMock(side_effect=failing_close)
    await worker.close()

    task = await repo.db.tasks.find_one({"id": "task"})
    operation = await repo.db.operations.find_one({"id": "operation"})
    assert task["status"] == ("COLLECTING" if superseded else "BLOCKED")
    assert operation["status"] == ("PENDING" if superseded else "FAILED")
    assert worker.active["task"] is runtime
    assert await repo.db.endpoint_locks.count_documents({}) == 1
