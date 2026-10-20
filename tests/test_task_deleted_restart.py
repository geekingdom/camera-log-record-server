"""软删除任务不得被 BLOCKED 恢复路径重新纳入调度。"""
# ruff: noqa: F811 - pytest 通过导入名称复用 test_api 的 client fixture。

import pytest
from camera_logs.common.database import now
from camera_logs.node.recovery import finalize_closed_task
from test_api import client  # noqa: F401
from test_task_control import _create_task
from test_task_recovery_receipts import _block_with_receipt


@pytest.mark.parametrize("deletion", ["task", "resource"])
def test_deleted_blocked_task_rejects_restart_without_reusing_historical_operation(client, deletion):
    """删除标记先于重启请求时，历史 PENDING 操作也不能作为幂等成功返回。"""
    task, repo = _create_task(client), client.app.state.repo
    _block_with_receipt(client, task)
    client.portal.call(repo.db.operations.insert_one, {
        "id": "historical-restart", "taskId": task["id"], "desiredState": "RUNNING",
        "action": "restart-blocked", "actor": "bootstrap", "status": "PENDING", "createdAt": now(),
    })
    if deletion == "task":
        client.portal.call(repo.db.tasks.update_one, {"id": task["id"]}, {"$set": {"deletedAt": now()}})
    else:
        client.portal.call(repo.db.tasks.update_one, {"id": task["id"]}, {"$set": {"resourceDeleted": True}})

    response = client.post(f"/api/v1/tasks/{task['id']}/restart")

    assert response.status_code == 409
    stored = client.portal.call(repo.db.tasks.find_one, {"id": task["id"]})
    assert stored.get("controlClaimVersion", 0) == 0
    assert (stored["status"], stored["desiredState"]) == ("BLOCKED", "RUNNING")
    assert not stored.get("restartRequested")
    operation = client.portal.call(repo.db.operations.find_one, {"id": "historical-restart"})
    assert operation["status"] == "PENDING"
    assert client.portal.call(repo.db.operations.count_documents,
                              {"taskId": task["id"], "action": "restart-blocked"}) == 1


@pytest.mark.parametrize("deletion", ["task", "resource"])
def test_deleted_blocked_task_rejects_isolation_confirmation(client, deletion):
    """管理员隔离确认同样不能把已删除任务的旧运行收尾为可重启状态。"""
    task, repo = _create_task(client), client.app.state.repo
    _block_with_receipt(client, task)
    if deletion == "task":
        client.portal.call(repo.db.tasks.update_one, {"id": task["id"]}, {"$set": {"deletedAt": now()}})
    else:
        client.portal.call(repo.db.tasks.update_one, {"id": task["id"]}, {"$set": {"resourceDeleted": True}})

    response = client.post(f"/api/v1/tasks/{task['id']}/restart", json={
        "confirmIsolation": True, "evidence": "旧会话已隔离并验证设备连接关闭",
    })

    assert response.status_code == 409
    stored = client.portal.call(repo.db.tasks.find_one, {"id": task["id"]})
    assert stored.get("controlClaimVersion", 0) == 0
    assert (stored["status"], stored["desiredState"], stored["nodeId"]) == (
        "BLOCKED", "RUNNING", "old-node",
    )
    assert client.portal.call(repo.db.operations.count_documents,
                              {"taskId": task["id"], "action": "restart-blocked"}) == 0


@pytest.mark.parametrize("deletion", ["task", "resource"])
def test_deleted_blocked_task_with_closed_receipt_releases_old_run_without_restart(client, deletion):
    """删除只取消重启资格，Worker 仍须消费精确收据并回收旧连接归属。"""
    repo = client.app.state.repo
    task_id = f"deleted-recovery-{deletion}"
    task = {
        "id": task_id, "runId": "old-run", "nodeId": "old-node", "generation": 7,
        "sessionId": "old-session", "protocol": "TELNET_DEVICE", "status": "BLOCKED",
        "desiredState": "RUNNING", "restartRequested": True, "controlOperationId": "historical-restart",
        "closedReceipt": {
            "taskId": task_id, "runId": "old-run", "nodeId": "old-node", "generation": 7,
            "sessionId": "old-session", "instanceId": "old-worker", "closedAt": now(),
        },
    }
    task["deletedAt" if deletion == "task" else "resourceDeleted"] = now() if deletion == "task" else True
    client.portal.call(repo.db.tasks.insert_one, task)
    client.portal.call(repo.db.runs.insert_one, {"id": "old-run", "taskId": task_id})
    client.portal.call(repo.db.endpoint_locks.insert_one, {"taskId": task_id, "runId": "old-run"})
    client.portal.call(repo.db.operations.insert_one, {
        "id": "historical-restart", "taskId": task_id, "desiredState": "RUNNING",
        "action": "restart-blocked", "status": "PENDING",
    })

    assert client.portal.call(finalize_closed_task, repo, task)

    stored = client.portal.call(repo.db.tasks.find_one, {"id": task_id})
    assert (stored["status"], stored["desiredState"], stored["nodeId"], stored["restartRequested"]) == (
        "STOPPED", "STOPPED", None, False,
    )
    assert client.portal.call(repo.db.endpoint_locks.count_documents, {"taskId": task_id}) == 0
    assert client.portal.call(repo.db.runs.find_one, {"id": "old-run"})["endedAt"]
    assert client.portal.call(repo.db.operations.find_one, {"id": "historical-restart"})["status"] == "CANCELLED"
