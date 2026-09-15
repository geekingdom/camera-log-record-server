"""节点级写入延迟准入限制的保存、调度和 Worker 回归。"""

import asyncio
import math
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.health import node_health, rank_nodes
from camera_logs.node.worker import Worker
from camera_logs.node.write_pressure import write_latency_blocked, write_latency_limit
from camera_logs.tasks.claim import claim_task
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient
from test_api import client  # noqa: F401
from test_node_health_selection import node
from test_scheduler_batch import _insert_pending_tasks, _repository


def test_write_latency_limit_uses_200ms_for_legacy_config_and_node_override():
    """旧登记缺失字段仍为200ms，新配置只能接受有限正整数毫秒值。"""
    assert write_latency_limit({}) == 200
    assert write_latency_limit({"writeLatencyLimitMs": 480}) == 480
    assert write_latency_limit({"writeLatencyLimitMs": 0}) == 200
    assert write_latency_limit({"writeLatencyLimitMs": True}) == 200
    assert write_latency_blocked({"writeLatencyMs": 200}) is False
    assert write_latency_blocked({"writeLatencyMs": 201}) is True
    assert write_latency_blocked({"writeLatencyMs": 300}, {"writeLatencyLimitMs": 480}) is False
    assert write_latency_blocked({"writeLatencyMs": 481}, {"writeLatencyLimitMs": 480}) is True
    assert write_latency_blocked({"writeLatencyMs": math.nan}) is False
    assert write_latency_blocked({"writeLatencyMs": math.inf}) is False


def test_health_and_ranking_use_node_specific_write_latency_limit():
    """告警从节点阈值的75%开始，候选排序不再被历史200ms硬编码截断。"""
    raised = node("raised") | {"writeLatencyMs": 300, "writeLatencyLimitMs": 400}
    reasons = node_health(raised)["reasons"]
    assert "接近限值" in " ".join(reasons)
    assert rank_nodes([raised], {})[0][1] == "raised"

    raised["writeLatencyLimitMs"] = 250
    assert "过高" in " ".join(node_health(raised)["reasons"])
    assert rank_nodes([raised], {}) == []


def test_node_write_latency_limit_is_versioned_and_audited(client):  # noqa: F811
    """管理员登记和编辑保存节点阈值，冲突和非法整数不会产生成功审计。"""
    created = client.post("/api/v1/admin/nodes", json={
        "id": "latency-node", "url": "http://latency-node:18081", "capacity": 100,
    })
    assert created.status_code == 201, created.text
    assert created.json()["writeLatencyLimitMs"] == 200

    updated = client.patch("/api/v1/admin/nodes/latency-node", json={
        "version": 1, "writeLatencyLimitMs": 480,
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["writeLatencyLimitMs"] == 480
    assert client.patch("/api/v1/admin/nodes/latency-node", json={
        "version": 1, "writeLatencyLimitMs": 600,
    }).status_code == 409
    assert client.patch("/api/v1/admin/nodes/latency-node", json={
        "version": 2, "writeLatencyLimitMs": 0,
    }).status_code == 422
    assert client.portal.call(client.app.state.repo.db.audit.count_documents, {
        "action": "update_node_config", "targetId": "latency-node",
    }) == 1


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_scheduler_and_claim_recheck_saved_write_latency_limit(tmp_path):
    """提高阈值立即允许旧心跳候选，随后收紧时领取事务拒绝新运行和锁。"""
    repo = await _repository(tmp_path)
    await _insert_pending_tasks(repo, 1)
    await repo.db.nodes.insert_one(node("latency") | {"writeLatencyMs": 300})
    await repo.db.node_configs.insert_one({"id": "latency", "writeLatencyLimitMs": 400})

    await schedule_once(repo)
    claimed = await repo.get("tasks", "task-0")
    assert claimed["nodeId"] == "latency"

    await repo.db.tasks.update_one({"id": "task-0"}, {"$set": {
        "nodeId": None, "runId": None, "status": "PENDING", "desiredState": "RUNNING",
    }})
    await repo.db.endpoint_locks.delete_many({})
    await repo.db.runs.delete_many({})
    await repo.db.node_configs.update_one({"id": "latency"}, {"$set": {"writeLatencyLimitMs": 250}})

    assert await claim_task(repo, await repo.get("tasks", "task-0"), "latency") is None
    assert await repo.db.runs.count_documents({}) == 0
    assert await repo.db.endpoint_locks.count_documents({}) == 0


async def test_worker_reloads_saved_write_latency_limit_without_stopping_existing_connection(tmp_path):
    """管理员放宽后心跳恢复准入；后续收紧只拒绝新会话，不关闭既有采集。"""
    repo = Repository(AsyncMongoMockClient().db, Settings(
        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
    ))
    await repo.initialize()
    task = {"id": "active", "runId": "run", "nodeId": repo.settings.node_id,
            "status": "COLLECTING", "desiredState": "RUNNING"}
    await repo.db.tasks.insert_one(task)
    await repo.db.node_configs.insert_one({"id": repo.settings.node_id, "writeLatencyLimitMs": 300})
    metrics = {"p99Ms": 250.0, "samples": 1, "pendingMs": 0.0, "windowSeconds": 60}
    runtime = SimpleNamespace(
        task=task, collector=SimpleNamespace(session_id="session", write_latency=SimpleNamespace(snapshot=lambda: dict(metrics))),
        input_bytes=0, stopping=False, error=None,
        background=asyncio.get_running_loop().create_future(), background_failure=lambda: None, stop=AsyncMock(),
    )
    worker = Worker(repo)
    worker.active[task["id"]] = runtime
    worker.last_maintenance = time.monotonic()
    disk = SimpleNamespace(used=50, total=100, free=50)

    with patch("camera_logs.node.worker.shutil.disk_usage", return_value=disk):
        await worker.tick()
        assert (await repo.get("nodes", repo.settings.node_id))["accepting"] is True
        runtime.stop.assert_not_awaited()

        await repo.db.node_configs.update_one(
            {"id": repo.settings.node_id}, {"$set": {"writeLatencyLimitMs": 200}},
        )
        await worker.tick()
        heartbeat = await repo.get("nodes", repo.settings.node_id)
        assert heartbeat["accepting"] is False
        assert heartbeat["writeLatencyLimitMs"] == 200
        runtime.stop.assert_not_awaited()
