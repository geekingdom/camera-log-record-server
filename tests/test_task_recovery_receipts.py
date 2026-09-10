"""BLOCKED 关闭收据消费的正式恢复接口回归。"""
# ruff: noqa: F811 - pytest fixture 通过导入名称复用。

from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.database import now
from test_api import client  # noqa: F401
from test_task_control import _create_task, _operation, _set_task, _task


def _task_operator_session(client):
    """用当前正式 API 夹具的管理员身份创建具备控制权限的普通创建者。"""
    created = client.post("/api/v1/users", json={
        "username": "receipt-operator", "displayName": "收据操作员", "password": "operator-password",
        "scopes": ["tasks:create", "tasks:control"],
    })
    assert created.status_code == 201, created.text
    del client.headers["Authorization"]
    logged_in = client.post("/api/v1/auth/login", json={
        "username": "receipt-operator", "password": "operator-password",
    }, headers={"X-Requested-With": "XMLHttpRequest"})
    assert logged_in.status_code == 200, logged_in.text
    changed = client.post("/api/v1/auth/password", json={
        "currentPassword": "operator-password", "newPassword": "operator-password-next",
    }, headers={"X-Requested-With": "XMLHttpRequest"})
    assert changed.status_code == 200, changed.text
    client.headers["X-Requested-With"] = "XMLHttpRequest"


def _block_with_receipt(client, task):
    """构造已关闭旧会话的精确收据，模拟 Worker 已完成传输层收尾。"""
    changes = {
        "status": "BLOCKED", "desiredState": "RUNNING", "nodeId": "old-node",
        "runId": "old-run", "generation": 7, "sessionId": "old-session",
    }
    receipt = {
        "taskId": task["id"], "runId": "old-run", "generation": 7, "nodeId": "old-node",
        "sessionId": "old-session", "instanceId": "old-worker", "closedAt": now(),
    }
    _set_task(client, task["id"], **changes, closedReceipt=receipt)


def test_receipt_consumption_reuses_existing_pending_restart_operation(client):
    """收据收尾复用已受理操作，不能生成第二个恢复请求。"""
    task, repo = _create_task(client), client.app.state.repo
    _block_with_receipt(client, task)
    client.portal.call(repo.db.operations.insert_one, {
        "id": "existing-restart", "taskId": task["id"], "desiredState": "RUNNING",
        "action": "restart-blocked", "actor": "bootstrap", "status": "PENDING", "createdAt": now(),
    })
    client.portal.call(repo.db.endpoint_locks.insert_one,
                       {"taskId": task["id"], "runId": "old-run", "endpoint": "127.0.0.1:22"})
    client.portal.call(repo.db.runs.insert_one, {"id": "old-run", "taskId": task["id"]})

    response = client.post(f"/api/v1/tasks/{task['id']}/restart")

    assert response.status_code == 202, response.text
    assert response.json()["id"] == "existing-restart"
    assert client.portal.call(repo.db.operations.count_documents,
                              {"taskId": task["id"], "action": "restart-blocked"}) == 1
    current = _task(client, task["id"])
    assert (current["status"], current["desiredState"], current["nodeId"]) == ("STOPPED", "RUNNING", None)
    assert client.portal.call(repo.db.endpoint_locks.find_one, {"taskId": task["id"]}) is None


def test_receipt_consumption_without_old_lock_releases_task_for_scheduler(client):
    """关闭收据不依赖残留锁；缺锁时也能结束旧 run 并重新排队。"""
    task, repo = _create_task(client), client.app.state.repo
    _block_with_receipt(client, task)
    client.portal.call(repo.db.runs.insert_one, {"id": "old-run", "taskId": task["id"]})

    response = client.post(f"/api/v1/tasks/{task['id']}/restart")

    assert response.status_code == 202, response.text
    current = _task(client, task["id"])
    assert (current["status"], current["desiredState"], current["nodeId"]) == ("STOPPED", "RUNNING", None)
    assert client.portal.call(repo.db.runs.find_one, {"id": "old-run"})["endedAt"]
    assert _operation(client, response.json()["id"])["status"] == "PENDING"


def test_foreign_lock_rejects_receipt_consumption_and_keeps_old_owner(client):
    """后继运行锁存在时，旧收据不能释放当前归属或创建恢复操作。"""
    task, repo = _create_task(client), client.app.state.repo
    _block_with_receipt(client, task)
    client.portal.call(repo.db.endpoint_locks.insert_one,
                       {"taskId": task["id"], "runId": "new-run", "endpoint": "127.0.0.1:22"})

    response = client.post(f"/api/v1/tasks/{task['id']}/restart")

    assert response.status_code == 409
    current = _task(client, task["id"])
    assert (current["status"], current["desiredState"], current["nodeId"], current["runId"]) == (
        "BLOCKED", "RUNNING", "old-node", "old-run"
    )
    assert client.portal.call(repo.db.endpoint_locks.find_one,
                              {"taskId": task["id"], "runId": "new-run"}) is not None
    assert client.portal.call(repo.db.operations.count_documents,
                              {"taskId": task["id"], "action": "restart-blocked"}) == 0


def test_non_admin_cannot_confirm_isolation_after_task_control_authorization(client):
    """具备任务控制权限的创建者仍不能调用管理员隔离确认。"""
    _task_operator_session(client)
    task = _create_task(client)
    _block_with_receipt(client, task)

    response = client.post(f"/api/v1/tasks/{task['id']}/restart", json={
        "confirmIsolation": True, "evidence": "operator verified the old session is fenced",
    })

    assert response.status_code == 403
    assert _task(client, task["id"])["status"] == "BLOCKED"


def test_repeated_accepted_restart_reuses_pending_operation(client):
    """等待在线 Worker 收尾的重复请求返回同一 pending operation。"""
    task, repo = _create_task(client), client.app.state.repo
    _set_task(client, task["id"], status="BLOCKED", desiredState="RUNNING", nodeId="live-node",
              runId="old-run", generation=7, sessionId="old-session")
    client.portal.call(repo.db.nodes.insert_one, {"id": "live-node", "heartbeat": now(), "accepting": True})

    first = client.post(f"/api/v1/tasks/{task['id']}/restart")
    second = client.post(f"/api/v1/tasks/{task['id']}/restart")

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert client.portal.call(repo.db.operations.count_documents,
                              {"taskId": task["id"], "action": "restart-blocked", "status": "PENDING"}) == 1


def test_restart_operation_waits_for_new_runtime_collecting_state(client):
    """旧运行释放后 operation 保持 pending，只有新会话采集成功才完成。"""
    task, repo = _create_task(client), client.app.state.repo
    _block_with_receipt(client, task)

    response = client.post(f"/api/v1/tasks/{task['id']}/restart")
    assert response.status_code == 202, response.text
    operation_id = response.json()["id"]
    assert _operation(client, operation_id)["status"] == "PENDING"

    scheduled = client.portal.call(repo.db.tasks.find_one, {"id": task["id"]})
    _set_task(client, task["id"], status="PENDING", nodeId="new-node", runId="new-run", generation=8)
    runtime = object.__new__(SessionRuntime)
    runtime.repo = repo
    runtime.task = scheduled | {"nodeId": "new-node", "runId": "new-run", "generation": 8}
    runtime.retired = False
    runtime.stopping = False
    client.portal.call(runtime.on_state, "COLLECTING", {"sessionId": "new-session"})

    assert _operation(client, operation_id)["status"] == "SUCCEEDED"
