"""节点健康解释与动态分配在同一指标边界上的回归。"""

from datetime import timedelta

import pytest
from camera_logs.common.database import now
from camera_logs.node.health import node_health, rank_nodes, resource_pressure
from camera_logs.tasks.claim import claim_task
from camera_logs.tasks.scheduler import schedule_once
from test_scheduler_batch import _insert_pending_tasks, _repository


def node(identifier, *, cpu=10, memory=20, capacity=100, upload=0):
    return {"id": identifier, "heartbeat": now(), "diskPercent": 20, "writeLatencyMs": 10,
            "capacity": capacity, "accepting": True, "activeTasks": 0,
            "telemetry": {"sampledAt": now(), "scope": "HOST", "cpuPercent": cpu,
                "memoryPercent": memory, "networkUploadBytesPerSecond": upload, "networkDownloadBytesPerSecond": 0}}


def test_health_reports_unknown_stale_offline_and_real_pressure():
    healthy = node("healthy")
    assert node_health(healthy)["status"] == "HEALTHY"
    busy = node("busy", cpu=97)
    assert node_health(busy)["status"] == "CRITICAL" and resource_pressure(busy)
    assert "CPU" in " ".join(node_health(busy)["reasons"])
    healthy["telemetry"]["sampledAt"] = now() - timedelta(seconds=31)
    assert node_health(healthy)["status"] == "UNKNOWN"
    healthy["heartbeat"] = now() - timedelta(seconds=31)
    assert node_health(healthy)["status"] == "OFFLINE"
    assert node_health(node("first", cpu=None))["status"] == "UNKNOWN"


def test_selection_uses_capacity_ratios_cpu_memory_and_network():
    small, large = node("small", capacity=10), node("large", capacity=100)
    assert rank_nodes([small, large], {"small": 5, "large": 10})[0][1] == "large"
    busy, quiet = node("busy", cpu=85, memory=80), node("quiet")
    assert rank_nodes([busy, quiet], {"busy": 0, "quiet": 1})[0][1] == "quiet"
    assert rank_nodes([node("a", upload=10000000), node("b")], {})[0][1] == "b"
    assert rank_nodes([node("cpu", cpu=95), node("memory", memory=95)], {}) == []
    assert rank_nodes([node("full", capacity=1)], {"full": 1}) == []


def test_selection_keeps_unknown_metrics_conservative():
    """节点排序只按节点负载；资源级 NFS 能力在领取事务中结合资源快照核验。"""
    measured, unknown = node("measured"), node("unknown")
    unknown.pop("telemetry")
    assert rank_nodes([unknown, measured], {})[0][1] == "measured"
    assert rank_nodes([unknown], {})[0][1] == "unknown"
    assert rank_nodes([node("zero", capacity=0)], {}) == []
    measured["telemetry"]["cpuPercent"] = 99
    measured["telemetry"]["sampledAt"] = now() - timedelta(minutes=1)
    assert rank_nodes([measured], {})[0][1] == "measured"


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_claim_rechecks_pressure_before_creating_run_or_lock(tmp_path):
    repo = await _repository(tmp_path)
    await _insert_pending_tasks(repo, 1)
    await repo.db.nodes.insert_one(node("changed", memory=99))
    task = await repo.get("tasks", "task-0")
    assert await claim_task(repo, task, "changed") is None
    assert await repo.db.endpoint_locks.count_documents({}) == 0
    assert await repo.db.runs.count_documents({}) == 0


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_scheduler_assigns_pending_task_to_lower_cost_node(tmp_path):
    """调度器须按实际领取结果选择低负载节点，而非仅验证 rank_nodes 输出。"""
    repo = await _repository(tmp_path)
    await _insert_pending_tasks(repo, 1)
    await repo.db.nodes.insert_many([node("busy", cpu=85, memory=80), node("quiet", cpu=10, memory=10)])
    await schedule_once(repo)
    assert (await repo.db.tasks.find_one({"id": "task-0"}))["nodeId"] == "quiet"
