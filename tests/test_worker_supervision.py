"""监督周期隔离回归：失去归属必须停连，过期收尾失败不能污染后继状态。"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.node.shutdown import SHUTDOWN_CONCURRENCY, shutdown_active_runtimes
from camera_logs.node.worker import Worker
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def setup_worker(tmp_path, monkeypatch):
    """显式提供测试密钥与磁盘余量，隔离开发机配置及后台维护。"""
    settings = Settings(
        _env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node"
    )
    repo = Repository(AsyncMongoMockClient().db, settings)
    task = {
        "id": "task",
        "runId": "run",
        "nodeId": "node",
        "generation": 1,
        "status": "COLLECTING",
        "desiredState": "RUNNING",
    }
    await repo.db.tasks.insert_one(task.copy())
    await repo.db.nodes.insert_one({"id": "node", "accepting": True})
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "run", "endpoint": "host:22"})
    runtime = SimpleNamespace(
        task=task,
        input_bytes=0,
        stopping=False,
        error=None,
        background=asyncio.get_running_loop().create_future(),
        background_failure=lambda: None,
        stop=AsyncMock(),
    )
    worker = Worker(repo)
    worker.active["task"] = runtime
    worker.last_maintenance = time.monotonic()
    monkeypatch.setattr(
        "camera_logs.node.worker.shutil.disk_usage", lambda _: SimpleNamespace(used=10, total=100, free=90)
    )
    return worker, repo, runtime


@pytest.mark.parametrize("change", ["missing", "node", "generation", "run", "blocked", "isolated"])
async def test_tick_closes_disowned_or_blocked_runtime_without_releasing_lock(tmp_path, monkeypatch, change):
    """隔离只证明本机连接关闭，不擅自放行任务锁或修改后继领取状态。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    if change == "missing":
        await repo.db.tasks.delete_one({"id": "task"})
    elif change == "isolated":
        await repo.db.nodes.update_one({"id": "node"}, {"$set": {"isolated": True}})
    else:
        changed = {
            "node": {"nodeId": "other"},
            "generation": {"generation": 2},
            "run": {"runId": "other"},
            "blocked": {"status": "BLOCKED"},
        }[change]
        await repo.db.tasks.update_one({"id": "task"}, {"$set": changed})
    before = await repo.db.tasks.find_one({"id": "task"})

    await worker.tick()
    await asyncio.gather(*worker.releases.values())

    runtime.stop.assert_awaited_once()
    assert "task" not in worker.active
    assert await repo.db.tasks.find_one({"id": "task"}) == before
    assert await repo.db.endpoint_locks.count_documents({}) == 1


@pytest.mark.parametrize("restart_requested", [False, True])
async def test_isolated_node_keeps_blocked_stop_or_restart_owner_records(
    tmp_path, monkeypatch, restart_requested
):
    """节点已隔离时，即使旧会话可关闭也不得按停止或恢复意图释放归属。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    await repo.db.tasks.update_one(
        {"id": "task"},
        {
            "$set": {
                "status": "BLOCKED",
                "desiredState": "STOPPED",
                "restartRequested": restart_requested,
                "sessionId": "old-session",
                "controlOperationId": "control",
            }
        },
    )
    await repo.db.runs.insert_one({"id": "run", "taskId": "task"})
    await repo.db.nodes.update_one({"id": "node"}, {"$set": {"isolated": True}})
    await repo.db.operations.insert_one(
        {
            "id": "control",
            "taskId": "task",
            "desiredState": "RUNNING" if restart_requested else "STOPPED",
            "status": "PENDING",
        }
    )
    before = await repo.db.tasks.find_one({"id": "task"})

    await worker.tick()
    await asyncio.gather(*worker.releases.values())

    runtime.stop.assert_awaited_once()
    assert await repo.db.tasks.find_one({"id": "task"}) == before
    assert await repo.db.endpoint_locks.count_documents({"taskId": "task", "runId": "run"}) == 1
    assert (await repo.db.runs.find_one({"id": "run"})).get("endedAt") is None
    assert (await repo.db.operations.find_one({"id": "control"}))["status"] == "PENDING"


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
    await repo.db.tasks.update_one(
        {"id": "task"},
        {
            "$set": {
                "nodeId": "other",
                "generation": 2,
                "desiredState": "RUNNING",
                "status": "COLLECTING",
            }
        },
    )
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


@pytest.mark.parametrize(
    ("desired_state", "expected_status", "expected_lock_count"),
    [
        ("RUNNING", "STOPPED", 0),
        ("PAUSED", "PAUSED", 1),
        ("STOPPED", "STOPPED", 0),
    ],
)
async def test_controlled_shutdown_preserves_persisted_user_intent(
    tmp_path, monkeypatch, desired_state, expected_status, expected_lock_count
):
    """容器受控退出关闭旧连接后，运行意图可重调度，暂停与停止意图不能被覆盖。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    await repo.db.runs.insert_one({"id": "run", "taskId": "task"})
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {"desiredState": desired_state}})

    await worker.close()

    stored = await repo.db.tasks.find_one({"id": "task"})
    assert runtime.stop.await_count == 1
    assert stored["desiredState"] == desired_state
    assert stored["status"] == expected_status and stored["nodeId"] is None
    assert await repo.db.endpoint_locks.count_documents({}) == expected_lock_count
    assert "task" not in worker.active


async def test_controlled_shutdown_does_not_release_blocked_owner(tmp_path, monkeypatch):
    """关闭阶段不能把未知隔离状态当成已确认连接关闭而解除任务锁。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {"status": "BLOCKED"}})
    before = await repo.db.tasks.find_one({"id": "task"})

    await worker.close()

    runtime.stop.assert_awaited_once()
    assert await repo.db.tasks.find_one({"id": "task"}) == before
    assert await repo.db.endpoint_locks.count_documents({}) == 1


async def test_controlled_shutdown_rereads_pause_requested_while_connection_closes(tmp_path, monkeypatch):
    """关闭期间的用户暂停必须覆盖旧的 RUNNING 快照，且继续保留运行和端点锁。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    started, continue_close = asyncio.Event(), asyncio.Event()

    async def delayed_stop():
        started.set()
        await continue_close.wait()

    runtime.stop = AsyncMock(side_effect=delayed_stop)
    closing = asyncio.create_task(worker.close())
    await asyncio.wait_for(started.wait(), 1)
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {"desiredState": "PAUSED"}})
    continue_close.set()
    await closing

    stored = await repo.db.tasks.find_one({"id": "task"})
    assert stored["desiredState"] == "PAUSED"
    assert stored["status"] == "PAUSED" and stored["nodeId"] is None
    assert await repo.db.endpoint_locks.count_documents({}) == 1
    assert await repo.db.runs.find_one({"id": "run"}) is None


async def test_controlled_shutdown_revokes_node_admission_before_connection_close(tmp_path, monkeypatch):
    """停止连接前先写入不接收标记，旧心跳尚未过期时调度器不得回填任务。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    observed = []

    async def inspect_node():
        node = await repo.db.nodes.find_one({"id": "node"})
        observed.append(node)

    runtime.stop = AsyncMock(side_effect=inspect_node)
    await worker.close()
    assert observed and observed[0]["accepting"] is False
    assert observed[0].get("shuttingDownAt") is not None


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_controlled_shutdown_running_task_is_reclaimed_by_next_worker(tmp_path, monkeypatch):
    """受控关闭确认旧连接后保留 RUNNING，下一节点周期可创建新的采集运行。"""
    worker, repo, _runtime = await setup_worker(tmp_path, monkeypatch)
    await repo.db.runs.insert_one({"id": "run", "taskId": "task"})
    await repo.db.tasks.update_one(
        {"id": "task"},
        {"$set": {"protocol": "TELNET_SERIAL", "ip": "127.0.0.1", "port": 23}},
    )

    await worker.close()
    await repo.db.nodes.insert_one(
        {"id": "next-node", "heartbeat": now(), "diskPercent": 10, "accepting": True, "capacity": 10}
    )
    await schedule_once(repo)

    restarted = await repo.db.tasks.find_one({"id": "task"})
    assert restarted["desiredState"] == "RUNNING"
    assert restarted["status"] == "PENDING" and restarted["nodeId"] == "next-node"
    assert restarted["runId"] != "run"


async def test_controlled_shutdown_closes_active_connections_while_prior_release_is_pending(tmp_path, monkeypatch):
    """已有收尾卡住时，退出仍须主动关闭其余采集连接，不能等待 Docker 强杀。"""
    worker, repo, _runtime = await setup_worker(tmp_path, monkeypatch)
    await repo.db.tasks.insert_one({
        "id": "other", "runId": "other-run", "nodeId": "node", "generation": 1,
        "status": "COLLECTING", "desiredState": "RUNNING",
    })
    await repo.db.runs.insert_one({"id": "other-run", "taskId": "other"})
    await repo.db.endpoint_locks.insert_one({"taskId": "other", "runId": "other-run", "endpoint": "host:23"})
    release_gate, stopped = asyncio.Event(), asyncio.Event()

    async def pending_release():
        await release_gate.wait()

    async def stop_other():
        stopped.set()

    pending = asyncio.create_task(pending_release())
    worker.releases["task"] = pending
    worker.active["other"] = SimpleNamespace(
        task={"id": "other", "runId": "other-run", "nodeId": "node", "generation": 1},
        input_bytes=0, stopping=False, error=None, background_failure=lambda: None, stop=AsyncMock(side_effect=stop_other),
    )
    closing = asyncio.create_task(worker.close())
    try:
        await asyncio.wait_for(stopped.wait(), .2)
        _runtime.stop.assert_not_awaited()
        assert not pending.done() and not pending.cancelled()
        assert (await repo.db.tasks.find_one({"id": "task"}))["status"] == "COLLECTING"
        assert await repo.db.endpoint_locks.count_documents({"taskId": "task", "runId": "run"}) == 1
    finally:
        release_gate.set()
        await asyncio.wait_for(closing, 1)


async def test_controlled_shutdown_cancellation_cleans_up_parallel_runtime_closure(tmp_path, monkeypatch):
    """退出被取消时，新增的剩余会话关闭任务必须一起取消，不能在 DB 关闭后继续运行。"""
    worker, _repo, _runtime = await setup_worker(tmp_path, monkeypatch)
    release_gate, shutdown_started, shutdown_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def pending_release():
        await release_gate.wait()

    async def slow_shutdown(*_args, **_kwargs):
        shutdown_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            shutdown_cancelled.set()
            raise

    pending = asyncio.create_task(pending_release())
    worker.releases["task"] = pending
    monkeypatch.setattr("camera_logs.node.shutdown.shutdown_active_runtimes", slow_shutdown)
    closing = asyncio.create_task(worker.close())
    await asyncio.wait_for(shutdown_started.wait(), 1)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert pending.cancelled()
    await asyncio.wait_for(shutdown_cancelled.wait(), 1)


async def test_shutdown_limits_concurrent_runtime_closures(tmp_path, monkeypatch):
    """优雅退出允许并行关闭，但不会一次向全部设备发起无限并发收尾。"""
    worker, _repo, _runtime = await setup_worker(tmp_path, monkeypatch)
    worker.active = {
        f"task-{index}": SimpleNamespace(task={"id": f"task-{index}"})
        for index in range(SHUTDOWN_CONCURRENCY + 1)
    }
    entered, release = asyncio.Event(), asyncio.Event()
    active = maximum = 0

    async def close_one(_worker, _runtime, *, allow_release=True):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == SHUTDOWN_CONCURRENCY:
            entered.set()
        await release.wait()
        active -= 1

    monkeypatch.setattr("camera_logs.node.shutdown.shutdown_runtime", close_one)
    closing = asyncio.create_task(shutdown_active_runtimes(worker))
    await asyncio.wait_for(entered.wait(), 1)
    assert maximum == SHUTDOWN_CONCURRENCY
    release.set()
    await closing
    assert maximum == SHUTDOWN_CONCURRENCY


@pytest.mark.parametrize("superseded", [False, True])
async def test_shutdown_failure_is_scoped_to_original_owner(tmp_path, monkeypatch, superseded):
    """进程退出期间关闭失败也必须使用旧归属，不能遗漏告警或污染后继。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    await repo.db.operations.insert_one({"id": "operation", "taskId": "task", "status": "PENDING"})

    async def failing_close():
        if superseded:
            await repo.db.tasks.update_one(
                {"id": "task"},
                {
                    "$set": {
                        "generation": 2,
                        "status": "COLLECTING",
                        "desiredState": "RUNNING",
                    }
                },
            )
        raise OSError("synthetic shutdown failure")

    runtime.stop = AsyncMock(side_effect=failing_close)
    await worker.close()

    task = await repo.db.tasks.find_one({"id": "task"})
    operation = await repo.db.operations.find_one({"id": "operation"})
    assert task["status"] == ("COLLECTING" if superseded else "BLOCKED")
    assert operation["status"] == ("PENDING" if superseded else "FAILED")
    assert worker.active["task"] is runtime
    assert await repo.db.endpoint_locks.count_documents({}) == 1
