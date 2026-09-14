"""资源级 Coredump 共享状态合同：仅当前有效 owner 才能锁定新任务开关。"""

from datetime import timedelta

import pytest
from camera_logs.common.database import now

pytest_plugins = ("test_resources",)


@pytest.mark.parametrize(
    ("resource_updates", "task_updates", "node_updates"),
    [
        ({"coredumpLeaseUntil": now() - timedelta(seconds=1)}, {}, {}),
        ({}, {"resourceId": "other-resource"}, {}),
        ({}, {"runId": "other-run"}, {}),
        ({}, {"generation": 9}, {}),
        ({}, {"protocol": "TELNET_SERIAL"}, {}),
        ({}, {"sshTarget": "SLAVE_1"}, {}),
        ({"enableCoredumpMonitor": False}, {}, {}),
        ({}, {"desiredState": "STOPPED"}, {}),
        ({}, {}, {"heartbeat": now() - timedelta(seconds=16)}),
    ],
)
def test_coredump_monitor_status_rejects_each_stale_owner_component(
    resource_client, resource_updates, task_updates, node_updates,
):
    """租约、任务和节点任一条件失效均必须返回固定 inactive 结构。"""
    repo = resource_client.app.state.repo
    stamp = now()
    resource = {
        "id": "camera", "kind": "HIKVISION_NETWORK", "deletedAt": None, "healthStatus": "ONLINE",
        "enableCoredumpMonitor": True,
        "coredumpLeaseTaskId": "owner", "coredumpLeaseRunId": "run-a", "coredumpLeaseGeneration": 4,
        "coredumpLeaseNodeId": "node-a", "coredumpLeaseUntil": stamp + timedelta(seconds=60),
    } | resource_updates
    task = {
        "id": "owner", "name": "采集", "resourceId": "camera", "runId": "run-a", "generation": 4,
        "nodeId": "node-a", "protocol": "SSH", "enableCoredumpMonitor": True,
        "desiredState": "RUNNING", "status": "COLLECTING",
    } | task_updates
    node = {"id": "node-a", "heartbeat": stamp} | node_updates
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    resource_client.portal.call(repo.db.tasks.insert_one, task)
    resource_client.portal.call(repo.db.nodes.insert_one, node)

    response = resource_client.get("/api/v1/resources/camera/coredump-monitor")

    assert response.status_code == 200
    assert response.json() == {"active": False, "ownerTask": None, "mountStatus": None}


def test_coredump_monitor_status_accepts_telnet_device_owner(resource_client):
    """Telnet 设备任务使用同一资源租约，并作为有效的 Coredump owner 展示。"""
    repo = resource_client.app.state.repo
    stamp = now()
    resource_client.portal.call(repo.db.resources.insert_one, {
        "id": "camera", "kind": "HIKVISION_NETWORK", "deletedAt": None, "healthStatus": "ONLINE",
        "enableCoredumpMonitor": True,
        "coredumpLeaseTaskId": "owner", "coredumpLeaseRunId": "run-a", "coredumpLeaseGeneration": 4,
        "coredumpLeaseNodeId": "node-a", "coredumpLeaseUntil": stamp + timedelta(seconds=60),
    })
    resource_client.portal.call(repo.db.tasks.insert_one, {
        "id": "owner", "name": "Telnet 值守", "resourceId": "camera", "runId": "run-a", "generation": 4,
        "nodeId": "node-a", "protocol": "TELNET_DEVICE", "enableCoredumpMonitor": True,
        "desiredState": "RUNNING", "status": "COLLECTING", "coredumpMountStatus": "MOUNTED",
        "coredumpMountRunId": "run-a",
    })
    resource_client.portal.call(repo.db.nodes.insert_one, {"id": "node-a", "heartbeat": stamp})

    response = resource_client.get("/api/v1/resources/camera/coredump-monitor")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "active": True, "ownerTask": {"id": "owner", "name": "Telnet 值守"}, "mountStatus": "MOUNTED",
    }
