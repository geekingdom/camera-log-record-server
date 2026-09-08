"""调度器必须使用节点心跳中的实际写入延迟决定新任务归属。"""

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

pytestmark = pytest.mark.usefixtures("mock_claim_transaction")


async def test_scheduler_waits_for_write_pressure_recovery_before_claiming_task(tmp_path):
    """高延迟心跳不领取任务，恢复到准入阈值后才向同一节点分配。"""
    repo = Repository(AsyncMongoMockClient().db, Settings(
        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))
    await repo.initialize()
    await repo.db.nodes.insert_one({
        "id": "node-a",
        "heartbeat": now(),
        "diskPercent": 10,
        "accepting": True,
        "capacity": 100,
        "writeLatencyMs": 201.0,
    })
    await repo.db.tasks.insert_one({
        "id": "pending",
        "ip": "127.0.0.1",
        "port": 22,
        "nodeId": None,
        "status": "PENDING",
        "desiredState": "RUNNING",
    })

    await schedule_once(repo)

    pending = await repo.get("tasks", "pending")
    assert pending["nodeId"] is None and pending["status"] == "PENDING"

    await repo.db.nodes.update_one({"id": "node-a"}, {"$set": {"writeLatencyMs": 200.0}})
    await schedule_once(repo)

    claimed = await repo.get("tasks", "pending")
    assert claimed["nodeId"] == "node-a" and claimed["status"] == "PENDING"
