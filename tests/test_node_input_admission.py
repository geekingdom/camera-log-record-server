"""输入吞吐硬准入的边界、动态配置和事务前复核。"""

from types import SimpleNamespace

import pytest
from camera_logs.node.health import rank_nodes
from camera_logs.node.input_admission import MIB, InputRateMeter, input_rate_blocked
from camera_logs.tasks.claim import claim_task
from camera_logs.tasks.scheduler import schedule_once
from test_api import client  # noqa: F401
from test_node_health_selection import node
from test_scheduler_batch import _insert_pending_tasks, _repository


def test_retired_runtime_cannot_cancel_remaining_input_rate():
    """大流量任务退出和同任务新运行的计数器归零均不能抵消其它输入。"""
    meter = InputRateMeter()
    meter.tick = 0
    large, small = SimpleNamespace(input_bytes=10 * MIB), SimpleNamespace(input_bytes=MIB)
    assert meter.sample([large, small], 1) == 11 * MIB
    large.input_bytes += MIB
    small.input_bytes += MIB
    assert meter.sample([small], 2) == 2 * MIB
    replacement = SimpleNamespace(input_bytes=MIB)
    assert meter.sample([replacement], 3) == MIB
    assert meter.sample([], 4) == 0
    assert not meter.previous


@pytest.mark.parametrize("rate,blocked", [(MIB - 1, False), (MIB, True), (MIB + 1, True), (None, True)])
def test_limit_boundary_and_unknown_measurement(rate, blocked):
    assert input_rate_blocked({"inputRateLimitMiB": 1, "inputBytesPerSecond": rate}) is blocked


def test_input_pressure_excludes_candidate_and_recovers_without_stopping_tasks():
    candidate = node("limited") | {"inputRateLimitMiB": 1, "inputBytesPerSecond": MIB}
    assert rank_nodes([candidate], {}) == []
    candidate["inputBytesPerSecond"] = MIB - 1
    assert rank_nodes([candidate], {})[0][1] == "limited"
    candidate["inputRateLimitMiB"] = 0
    candidate["inputBytesPerSecond"] = 100 * MIB
    assert rank_nodes([candidate], {})[0][1] == "limited"


def test_node_limit_defaults_and_updates_are_versioned(client):  # noqa: F811
    created = client.post("/api/v1/admin/nodes", json={"id": "rate-node", "url": "http://worker:18081", "capacity": 100})
    assert created.status_code == 201, created.text
    assert created.json()["inputRateLimitMiB"] == 50
    updated = client.patch("/api/v1/admin/nodes/rate-node", json={"version": 1, "inputRateLimitMiB": 24})
    assert updated.status_code == 200, updated.text
    assert updated.json()["inputRateLimitMiB"] == 24
    assert client.patch("/api/v1/admin/nodes/rate-node", json={"version": 1, "inputRateLimitMiB": 0}).status_code == 409
    assert client.patch("/api/v1/admin/nodes/rate-node", json={"version": 2, "inputRateLimitMiB": -1}).status_code == 422


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_scheduler_uses_saved_limit_and_claim_rechecks_new_limit(tmp_path):
    """不能信任旧心跳中的配置；候选选出后管理员收紧限制也必须阻止领取。"""
    repo = await _repository(tmp_path)
    await _insert_pending_tasks(repo, 1)
    await repo.db.nodes.insert_one(node("rate") | {"inputBytesPerSecond": 2 * MIB, "inputRateLimitMiB": 0})
    await repo.db.node_configs.insert_one({"id": "rate", "inputRateLimitMiB": 1})
    await schedule_once(repo)
    assert (await repo.get("tasks", "task-0")).get("nodeId") is None
    task = await repo.get("tasks", "task-0")
    assert await claim_task(repo, task, "rate") is None
    assert await repo.db.runs.count_documents({}) == 0
    assert await repo.db.endpoint_locks.count_documents({}) == 0
    await repo.db.node_configs.update_one({"id": "rate"}, {"$set": {"inputRateLimitMiB": 3}})
    await schedule_once(repo)
    assert (await repo.get("tasks", "task-0"))["nodeId"] == "rate"
