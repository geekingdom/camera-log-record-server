"""认证及主从引导审计必须有可读摘要，列表和数据库筛选使用相同结果语义。"""

import pytest

pytest_plugins = ("test_api",)


@pytest.mark.usefixtures("awaitable_mongomock_event_aggregate")
@pytest.mark.parametrize("action,summary,outcome,level", [
    ("authenticate_resource_succeeded", "设备身份认证成功", "SUCCEEDED", "INFO"),
    ("authenticate_resource_credentials_rejected", "设备身份认证失败：凭据被拒绝", "FAILED", "ERROR"),
    ("authenticate_resource_device_error", "设备身份认证失败：离线或设备异常", "FAILED", "ERROR"),
    ("slave-ssh-bootstrap-host-attempt", "借用主机连接引导从机SSH服务，等待确认", "UNKNOWN", "WARNING"),
    ("slave-ssh-bootstrap-temporary-attempt", "通过临时连接引导从机SSH服务，等待确认", "UNKNOWN", "WARNING"),
    ("slave-ssh-service-ready", "从机共享SSH服务已就绪", "SUCCEEDED", "INFO"),
    ("slave-ssh-connected", "已进入指定从机SSH会话", "SUCCEEDED", "INFO"),
])
def test_device_action_summary_and_derived_filter_agree(client, action, summary, outcome, level):
    """通过正式审计写入器记录动作，不能将失败或仅尝试误标为成功并漏出失败筛选。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.resources.insert_one, {
        "id": "device-action-resource", "name": "验证设备", "ip": "192.0.2.81",
    })
    client.portal.call(repo.audit, "bootstrap", action, "device-action-resource")
    unfiltered = client.get("/api/v1/audit-events", params={"action": action})
    assert unfiltered.status_code == 200
    item = unfiltered.json()["items"][0]
    assert (item["summary"], item["outcome"], item["level"]) == (summary, outcome, level)
    assert item["targetName"] == "验证设备" and item["deviceIp"] == "192.0.2.81"
    filtered = client.get("/api/v1/audit-events", params={"action": action, "outcome": outcome, "level": level})
    assert filtered.status_code == 200 and filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["summary"] == summary
    other = "SUCCEEDED" if outcome != "SUCCEEDED" else "FAILED"
    excluded = client.get("/api/v1/audit-events", params={"action": action, "outcome": other})
    assert excluded.json()["total"] == 0


@pytest.mark.usefixtures("awaitable_mongomock_event_aggregate")
def test_slave_bootstrap_runtime_event_keeps_historical_route_context(client):
    """按节点和未知结果查询引导事件时仍返回历史主机与端口，不依赖当前任务归属。"""
    from camera_logs.common.database import now

    repo = client.app.state.repo
    client.portal.call(repo.db.events.insert_one, {
        "type": "SLAVE_SSH_BOOTSTRAP", "createdAt": now(), "taskId": "slave",
        "runId": "run", "generation": 2, "resourceId": "device", "nodeId": "caller",
        "hostTaskId": "host", "hostNodeId": "remote", "port": 18080,
        "phase": "REMOTE_UNKNOWN", "outcome": "UNKNOWN", "level": "WARNING",
        "message": "跨节点引导结果未知，保留资源租约",
    })
    response = client.get("/api/v1/runtime-events", params={"nodeId": "caller", "outcome": "UNKNOWN"})
    assert response.status_code == 200 and response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["summary"] == "从机SSH连接与引导状态"
    assert (item["hostTaskId"], item["hostNodeId"], item["port"]) == ("host", "remote", 18080)
    assert item["reason"] == "跨节点引导结果未知，保留资源租约"
