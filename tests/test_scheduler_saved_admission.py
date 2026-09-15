"""管理员已保存的准入与容量必须覆盖尚未刷新的节点心跳。"""

import pytest
from camera_logs.tasks.claim import claim_task
from camera_logs.tasks.scheduler import schedule_once
from test_node_health_selection import node
from test_scheduler_batch import _insert_pending_tasks, _repository

pytestmark = pytest.mark.usefixtures("mock_claim_transaction")


@pytest.mark.parametrize("config,occupied", [({"accepting": False}, 0), ({"capacity": 1}, 1)])
async def test_claim_rechecks_saved_admission_before_new_run(tmp_path, config, occupied):
    """关闭准入或收紧容量后，旧心跳不能使事务建立新运行及端点锁。"""
    repo = await _repository(tmp_path)
    await _insert_pending_tasks(repo, 1)
    await repo.db.nodes.insert_one(node("limited") | {"capacity": 10, "accepting": True})
    await repo.db.node_configs.insert_one({"id": "limited", **config})
    assert await claim_task(repo, await repo.get("tasks", "task-0"), "limited", occupied=occupied) is None
    assert await repo.db.runs.count_documents({}) == 0
    assert await repo.db.endpoint_locks.count_documents({}) == 0


async def test_scheduler_skips_disabled_node_before_heartbeat_refresh(tmp_path):
    """A心跳仍允许时，已关闭的配置必须使本周期直接选择可用的B。"""
    repo = await _repository(tmp_path)
    await _insert_pending_tasks(repo, 1)
    await repo.db.nodes.insert_many([node("a"), node("b")])
    await repo.db.node_configs.insert_one({"id": "a", "accepting": False})
    await schedule_once(repo)
    assert (await repo.get("tasks", "task-0"))["nodeId"] == "b"


async def test_claim_uses_increased_saved_capacity_before_heartbeat_refresh(tmp_path):
    """已有有效健康心跳时，调高容量不应继续被旧上限截断。"""
    repo = await _repository(tmp_path)
    await _insert_pending_tasks(repo, 1)
    await repo.db.nodes.insert_one(node("raised") | {"capacity": 1})
    await repo.db.node_configs.insert_one({"id": "raised", "capacity": 2})
    assert await claim_task(repo, await repo.get("tasks", "task-0"), "raised", occupied=1)


@pytest.mark.parametrize("heartbeat_capacity,saved_capacity", [(1, 2), (10, 1)])
async def test_scheduler_enforces_saved_capacity_for_entire_batch(tmp_path, heartbeat_capacity, saved_capacity):
    """扩容与缩容均以保存值完成候选筛选和批次分配，不等待旧心跳刷新。"""
    repo = await _repository(tmp_path)
    await _insert_pending_tasks(repo, 3)
    await repo.db.nodes.insert_one(node("configured", capacity=heartbeat_capacity))
    await repo.db.node_configs.insert_one({"id": "configured", "capacity": saved_capacity})
    await schedule_once(repo)
    assert await repo.db.tasks.count_documents({"nodeId": "configured"}) == saved_capacity
    assert await repo.db.runs.count_documents({}) == saved_capacity
