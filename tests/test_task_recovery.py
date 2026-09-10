"""BLOCKED 任务受控恢复与单任务隔离的接口回归。"""
# ruff: noqa: F811

from datetime import timedelta

from camera_logs.common.database import now
from camera_logs.node.recovery import matching_closed_receipt
from test_api import client  # noqa: F401
from test_task_control import _create_task, _operation, _set_task, _task


def test_blocked_start_rejects_and_unreachable_restart_requires_isolation(client):
    """不能把 BLOCKED 静默排队；旧节点失联时必须明确要求隔离确认。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="BLOCKED", desiredState="RUNNING", nodeId="lost", runId="old", generation=1)

    assert client.post(f"/api/v1/tasks/{task['id']}/start").status_code == 409
    response = client.post(f"/api/v1/tasks/{task['id']}/restart")
    assert response.status_code == 409
    assert "ISOLATION_REQUIRED" in response.text


def test_restart_keeps_operation_pending_until_new_runtime_collects(client):
    """旧运行收尾只恢复调度资格；成功必须等待新 run 的 COLLECTING 状态。"""
    task, repo = _create_task(client), client.app.state.repo
    stamp = now()
    _set_task(client, task["id"], status="BLOCKED", desiredState="RUNNING", nodeId="live", runId="old", generation=1)
    client.portal.call(repo.db.nodes.insert_one, {"id": "live", "heartbeat": stamp, "accepting": True})

    response = client.post(f"/api/v1/tasks/{task['id']}/restart")
    assert response.status_code == 202, response.text
    operation = _operation(client, response.json()["id"])
    current = _task(client, task["id"])
    assert operation["status"] == "PENDING"
    assert (current["status"], current["desiredState"], current["restartRequested"]) == ("BLOCKED", "STOPPED", True)


def test_admin_confirmation_releases_only_matching_task_lock_and_redacts_evidence(client):
    """确认隔离不影响同节点其它任务，证据不得保留口令正文。"""
    task, repo = _create_task(client), client.app.state.repo
    other = _create_task(client)
    _set_task(client, task["id"], status="BLOCKED", nodeId="live", runId="old", generation=1, desiredState="STOPPED")
    _set_task(client, other["id"], status="BLOCKED", nodeId="live", runId="other", generation=1, desiredState="STOPPED")
    client.portal.call(repo.db.endpoint_locks.insert_many, [
        {"taskId": task["id"], "runId": "old"}, {"taskId": other["id"], "runId": "other"},
    ])
    client.portal.call(repo.db.runs.insert_one, {"id": "old", "taskId": task["id"]})

    response = client.post(f"/api/v1/tasks/{task['id']}/restart", json={
        "confirmIsolation": True, "evidence": "password=secret external fence verified",
    })
    assert response.status_code == 202, response.text
    assert client.portal.call(repo.db.endpoint_locks.find_one, {"taskId": task["id"]}) is None
    assert client.portal.call(repo.db.endpoint_locks.find_one, {"taskId": other["id"], "runId": "other"}) is not None
    assert "secret" not in _task(client, task["id"])["isolationEvidence"]


def test_restart_rejects_resource_health_failure(client):
    """恢复与普通启动相同，资源离线或认证失败不能绕过 guard。"""
    task, repo = _create_task(client), client.app.state.repo
    _set_task(client, task["id"], status="BLOCKED", nodeId="live", runId="old", generation=1)
    client.portal.call(repo.db.nodes.insert_one, {"id": "live", "heartbeat": now() - timedelta(seconds=1)})
    client.portal.call(repo.db.resources.update_one, {"id": "fixture-device"}, {"$set": {"healthStatus": "OFFLINE"}})
    assert client.post(f"/api/v1/tasks/{task['id']}/restart").status_code == 409


def test_closed_receipt_requires_every_exact_owner_field():
    """旧收据必须绑定任务、运行、代次、节点和实际会话，缺任一字段都不可消费。"""
    task = {"id": "task", "runId": "run", "generation": 3, "nodeId": "node", "sessionId": "session"}
    receipt = {"taskId": "task", "runId": "run", "generation": 3, "nodeId": "node",
               "sessionId": "session", "instanceId": "worker", "closedAt": now()}
    assert matching_closed_receipt(task | {"closedReceipt": receipt})
    for key in ("taskId", "runId", "generation", "nodeId", "sessionId", "instanceId", "closedAt"):
        broken = dict(receipt)
        broken[key] = None
        assert not matching_closed_receipt(task | {"closedReceipt": broken})


def test_confirmation_rejects_blank_evidence(client):
    """管理员确认隔离必须留下非空、可审计的证据文本。"""
    task = _create_task(client)
    response = client.post(f"/api/v1/tasks/{task['id']}/restart", json={
        "confirmIsolation": True, "evidence": "          ",
    })
    assert response.status_code == 422
