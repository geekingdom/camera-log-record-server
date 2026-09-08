"""调度器批量选点的查询复杂度和容量归属回归测试。"""

from datetime import timedelta

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.tasks.scheduler import reconcile_stopped_tasks, schedule_once
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

pytestmark = pytest.mark.usefixtures("mock_claim_transaction")


async def _repository(tmp_path, *, cluster_capacity=800):
    """创建独立的内存仓库，避免调度测试依赖真实节点或设备。"""
    repo = Repository(
        AsyncMongoMockClient().db,
        Settings(
            encryption_key=Fernet.generate_key().decode(),
            log_root=tmp_path,
            cluster_capacity=cluster_capacity,
        ),
    )
    await repo.initialize()
    return repo


async def _insert_nodes(repo, count, *, capacity=100):
    """写入满足调度准入条件的节点，并显式声明容量和写入指标。"""
    await repo.db.nodes.insert_many([
        {
            "id": f"node-{index}",
            "heartbeat": now(),
            "diskPercent": 10,
            "accepting": True,
            "capacity": capacity,
            "writeLatencyMs": 0,
            "inputBytesPerSecond": 0,
        }
        for index in range(count)
    ])


async def _insert_pending_tasks(repo, count):
    """写入可领取的待运行任务，每项使用不同端口以隔离运行锁影响。"""
    await repo.db.tasks.insert_many([
        {
            "id": f"task-{index}",
            "ip": "127.0.0.1",
            "port": 20000 + index,
            "nodeId": None,
            "status": "PENDING",
            "desiredState": "RUNNING",
        }
        for index in range(count)
    ])


async def test_settled_history_does_not_generate_per_task_writes(tmp_path, monkeypatch):
    """历史停止任务不应消耗调度租约内的逐项数据库往返预算。"""
    repo = await _repository(tmp_path)
    await _insert_nodes(repo, 1)
    await _insert_pending_tasks(repo, 1)
    await repo.db.tasks.insert_many([
        {"id": f"history-{i}", "desiredState": "STOPPED", "status": "STOPPED", "nodeId": None}
        for i in range(1000)
    ])
    collection_type = type(repo.db.tasks)
    original = collection_type.update_one
    writes = 0

    async def count_writes(self, *args, **kwargs):
        nonlocal writes
        if self.name == "tasks":
            writes += 1
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(collection_type, "update_one", count_writes)
    await schedule_once(repo)
    assert (await repo.get("tasks", "task-0"))["nodeId"] == "node-0"
    assert writes <= 2


async def test_stop_reconciliation_preserves_terminal_errors_and_running_intent(tmp_path):
    """仅无节点且确认停止的任务完成停止操作，异常及新启动意图不能被覆盖。"""
    repo = await _repository(tmp_path, cluster_capacity=0)
    cases = [("stopped", "STOPPED", "STOPPED", None),
             ("pending", "PENDING", "STOPPED", None),
             ("blocked", "BLOCKED", "STOPPED", None),
             ("error", "ERROR", "STOPPED", None),
             ("running", "PENDING", "RUNNING", None),
             ("owned", "COLLECTING", "STOPPED", "worker")]
    for identifier, status, desired, owner in cases:
        await repo.db.tasks.insert_one({"id": identifier, "status": status,
                                       "desiredState": desired, "nodeId": owner})
        await repo.db.operations.insert_one({"id": identifier, "taskId": identifier,
                                            "desiredState": "STOPPED", "status": "PENDING"})
    await schedule_once(repo)
    for identifier, status, _, _ in cases:
        completed = identifier in {"stopped", "pending"}
        task = await repo.get("tasks", identifier)
        operation = await repo.get("operations", identifier)
        assert task["status"] == ("STOPPED" if completed else status)
        assert operation["status"] == ("SUCCEEDED" if completed else "PENDING")


async def test_stop_reconciliation_does_not_overwrite_cancelled_operation(tmp_path, monkeypatch):
    """关联查询后发生启动请求时，收尾不得覆盖已取消操作或新的运行意图。"""
    repo = await _repository(tmp_path)
    await repo.db.tasks.insert_one({"id": "task", "desiredState": "STOPPED",
                                    "status": "STOPPED", "nodeId": None})
    await repo.db.operations.insert_one({"id": "stop", "taskId": "task",
                                         "desiredState": "STOPPED", "status": "PENDING"})
    collection_type = type(repo.db.operations)
    original = collection_type.update_many

    async def restart_before_operation_update(self, query, update, **kwargs):
        if self.name == "operations":
            await repo.db.operations.update_one({"id": "stop"}, {"$set": {"status": "CANCELLED"}})
            await repo.db.tasks.update_one({"id": "task"}, {"$set": {"desiredState": "RUNNING"}})
        return await original(self, query, update, **kwargs)

    monkeypatch.setattr(collection_type, "update_many", restart_before_operation_update)
    await reconcile_stopped_tasks(repo.db)
    assert (await repo.get("operations", "stop"))["status"] == "CANCELLED"
    assert (await repo.get("tasks", "task"))["desiredState"] == "RUNNING"


async def test_schedule_once_batch_query_budget_and_capacity_assignment(tmp_path, monkeypatch):
    """500 个任务和 8 个节点时，查询次数必须不随任务和节点的乘积增长。"""
    repo = await _repository(tmp_path)
    await _insert_nodes(repo, 8)
    await _insert_pending_tasks(repo, 500)

    counters = {"nodes_find": 0, "task_counts": 0}
    node_collection_type = type(repo.db.nodes)
    task_collection_type = type(repo.db.tasks)
    original_find = node_collection_type.find
    original_count_documents = task_collection_type.count_documents

    def count_node_find(self, *args, **kwargs):
        """仅记录节点集合查询，游标创建仍交给 MongoMock 原实现。"""
        if self.name == "nodes":
            counters["nodes_find"] += 1
        return original_find(self, *args, **kwargs)

    async def count_task_documents(self, *args, **kwargs):
        """仅记录任务计数，确保统计不改变调度器的返回值。"""
        if self.name == "tasks":
            counters["task_counts"] += 1
        return await original_count_documents(self, *args, **kwargs)

    monkeypatch.setattr(node_collection_type, "find", count_node_find)
    monkeypatch.setattr(task_collection_type, "count_documents", count_task_documents)

    await schedule_once(repo)
    schedule_counters = counters.copy()

    assigned = await repo.db.tasks.count_documents({"nodeId": {"$ne": None}})
    assert assigned == 500
    for index in range(8):
        assert await repo.db.tasks.count_documents({"nodeId": f"node-{index}"}) <= 100

    # 节点仍逐任务检查最新准入，任务占用统计不能重复为每个候选节点执行。
    assert schedule_counters["nodes_find"] == 501
    assert schedule_counters["task_counts"] <= 2


async def test_schedule_once_counts_fresh_and_stale_ownership_against_cluster_capacity(tmp_path):
    """不同节点和已失联节点的现有归属都必须占用集群容量。"""
    repo = await _repository(tmp_path, cluster_capacity=500)
    await _insert_nodes(repo, 1)
    await repo.db.nodes.insert_one({
        "id": "stale-node",
        "heartbeat": now() - timedelta(seconds=31),
        "diskPercent": 10,
        "accepting": True,
        "capacity": 100,
    })
    await _insert_pending_tasks(repo, 1)
    await repo.db.tasks.insert_many([
        {
            "id": f"fresh-owned-{index}",
            "ip": "192.0.2.10",
            "port": 30000 + index,
            "nodeId": "node-0",
            "status": "PENDING",
            "desiredState": "RUNNING",
        }
        for index in range(499)
    ] + [{
        "id": "stale-owned",
        "ip": "192.0.2.11",
        "port": 30500,
        "nodeId": "stale-node",
        "status": "PENDING",
        "desiredState": "RUNNING",
    }])

    await schedule_once(repo)

    pending = await repo.get("tasks", "task-0")
    stale_owned = await repo.get("tasks", "stale-owned")
    assert pending["nodeId"] is None
    assert stale_owned["status"] == "BLOCKED"


async def test_schedule_once_successful_claim_updates_capacity_budget(tmp_path):
    """领取成功后必须更新本周期容量预算，单节点容量不能被后续任务突破。"""
    repo = await _repository(tmp_path)
    await _insert_nodes(repo, 2, capacity=1)
    await _insert_pending_tasks(repo, 2)

    await schedule_once(repo)

    assignments = [
        (await repo.get("tasks", f"task-{index}"))["nodeId"]
        for index in range(2)
    ]
    assert sorted(assignments) == ["node-0", "node-1"]
    for node_id in assignments:
        assert await repo.db.tasks.count_documents({"nodeId": node_id}) == 1


async def test_schedule_once_rechecks_accepting_after_previous_claim(tmp_path, monkeypatch):
    """节点在首项领取后停止接收时，同周期的后续任务不得继续分配。"""
    repo = await _repository(tmp_path)
    await _insert_nodes(repo, 1, capacity=2)
    await _insert_pending_tasks(repo, 2)
    collection_type = type(repo.db.tasks)
    original_claim = collection_type.find_one_and_update
    accepting_disabled = False

    async def disable_accepting_after_first_claim(self, query, update, **kwargs):
        """首个普通领取完成后模拟节点心跳报告停止接收新任务。"""
        nonlocal accepting_disabled
        claimed = await original_claim(self, query, update, **kwargs)
        is_task_claim = (
            self.name == "tasks"
            and query.get("nodeId") is None
            and query.get("desiredState") == "RUNNING"
            and "id" in query
        )
        if claimed and is_task_claim and not accepting_disabled:
            accepting_disabled = True
            await repo.db.nodes.update_one({"id": "node-0"}, {"$set": {"accepting": False}})
        return claimed

    monkeypatch.setattr(collection_type, "find_one_and_update", disable_accepting_after_first_claim)

    await schedule_once(repo)

    first = await repo.get("tasks", "task-0")
    second = await repo.get("tasks", "task-1")
    assert accepting_disabled
    assert first["nodeId"] == "node-0"
    assert second["nodeId"] is None


async def test_schedule_once_failed_claim_does_not_consume_capacity_budget(tmp_path, monkeypatch):
    """原子领取失败时不得扣减本周期节点预算，后续任务仍可领取该节点。"""
    repo = await _repository(tmp_path)
    await _insert_nodes(repo, 1, capacity=1)
    await _insert_pending_tasks(repo, 2)
    collection_type = type(repo.db.tasks)
    original_claim = collection_type.find_one_and_update
    failed = False

    async def fail_first_task_claim(self, query, update, **kwargs):
        """只模拟第一次普通任务归属 CAS 失败，其余数据库操作保持原样。"""
        nonlocal failed
        is_task_claim = (
            self.name == "tasks"
            and query.get("nodeId") is None
            and query.get("desiredState") == "RUNNING"
            and "id" in query
        )
        if is_task_claim and not failed:
            failed = True
            return None
        return await original_claim(self, query, update, **kwargs)

    monkeypatch.setattr(collection_type, "find_one_and_update", fail_first_task_claim)

    await schedule_once(repo)

    first = await repo.get("tasks", "task-0")
    second = await repo.get("tasks", "task-1")
    assert failed
    assert first["nodeId"] is None
    assert second["nodeId"] == "node-0"
    assert await repo.db.tasks.count_documents({"nodeId": "node-0"}) == 1
