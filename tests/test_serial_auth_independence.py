"""串口 Telnet 不受海康 HTTP 认证状态影响的任务准入回归。"""
# ruff: noqa: F811 - pytest 按名称注入复用的 client fixture。

from uuid import uuid4

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.tasks.claim import claim_task
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient
from test_api import client  # noqa: F401


def _serial_body(name, *, auto_start):
    """构造使用海康资源作为被采集设备的串口 Telnet 请求。"""
    return {"name": name, "protocol": "TELNET_SERIAL", "ip": "127.0.0.1", "port": 9000,
            "resourceId": "fixture-device", "autoStart": auto_start}


@pytest.mark.parametrize("health_status", ["AUTH_FAILED", "OFFLINE", "ERROR"])
def test_bad_network_health_allows_serial_auto_create_and_manual_start(client, health_status):
    """三类 ISAPI 非成功状态不得阻止串口任务创建或改变其运行意图。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.resources.update_one, {"id": "fixture-device"},
                       {"$set": {"healthStatus": health_status}})

    created = client.post("/api/v1/tasks", json=_serial_body(f"auto-{health_status}", auto_start=True),
                          headers={"Idempotency-Key": uuid4().hex})
    assert created.status_code == 201, created.text
    assert created.json()["desiredState"] == "RUNNING"

    manual = client.post("/api/v1/tasks", json=_serial_body(f"manual-{health_status}", auto_start=False),
                         headers={"Idempotency-Key": uuid4().hex})
    assert manual.status_code == 201, manual.text
    started = client.post(f"/api/v1/tasks/{manual.json()['id']}/start")
    assert started.status_code == 202, started.text
    current = client.get(f"/api/v1/tasks/{manual.json()['id']}").json()
    assert current["desiredState"] == "RUNNING"


@pytest.mark.usefixtures("mock_claim_transaction")
@pytest.mark.parametrize("health_status", ["AUTH_FAILED", "OFFLINE", "ERROR"])
async def test_bad_network_health_allows_serial_claim(tmp_path, health_status):
    """调度领取串口任务仍须校验节点和资源删除，但不读取 HTTP 健康作为准入条件。"""
    repo = Repository(AsyncMongoMockClient().db, Settings(
        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
    ))
    await repo.initialize()
    await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "deletedAt": None,
                                        "ip": "192.0.2.80", "healthStatus": health_status})
    task = {"id": "serial", "resourceId": "camera", "protocol": "TELNET_SERIAL", "ip": "192.0.2.80",
            "port": 9000, "nodeId": None, "status": "STOPPED", "desiredState": "RUNNING", "generation": 0}
    await repo.db.tasks.insert_one(task)
    await repo.db.nodes.insert_one({"id": "node", "heartbeat": now(), "diskPercent": 10, "accepting": True,
                                    "capacity": 10, "writeLatencyMs": 0, "inputBytesPerSecond": 0})

    claimed = await claim_task(repo, task, "node")
    assert claimed is not None
    current = await repo.db.tasks.find_one({"id": "serial"})
    assert (current["nodeId"], current["desiredState"]) == ("node", "RUNNING")
