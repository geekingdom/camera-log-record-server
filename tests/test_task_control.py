"""任务控制 API 的公开状态语义。

MongoMock 仅用于构造运行、锁和预算的前置状态，不模拟事务回滚或并发仲裁；
相同期望状态的并发写入由真实 MongoDB 验证脚本覆盖。
"""
# ruff: noqa: F811 - pytest 通过导入名称复用 test_api 的 client fixture。

from uuid import uuid4

import pytest
from camera_logs.common.database import now
from test_api import client  # noqa: F401


def _create_task(client, *, protocol="SSH"):
    """通过正式创建接口生成可控制任务，避免测试伪造资源绑定字段。"""
    body = {
        "name": f"control-{uuid4().hex}",
        "protocol": protocol,
        "ip": "127.0.0.1",
        "port": 22 if protocol == "SSH" else 9000,
        "resourceId": "fixture-device",
    }
    if protocol != "TELNET_SERIAL":
        body.update(username="root", password="secret")
    response = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": uuid4().hex})
    assert response.status_code == 201, response.text
    return response.json()


def _set_task(client, task_id, **changes):
    """只设置控制接口无法自行到达的 worker 运行前置状态。"""
    client.portal.call(
        client.app.state.repo.db.tasks.update_one,
        {"id": task_id},
        {"$set": changes},
    )


def _task(client, task_id):
    """经正式 GET 接口读取公开任务状态。"""
    response = client.get(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _operation(client, operation_id):
    """经正式 GET 接口读取异步操作，不依赖集合内部表示。"""
    response = client.get(f"/api/v1/operations/{operation_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _control(client, task_id, action):
    """调用正式控制端点并断言统一的异步接受状态。"""
    return client.post(f"/api/v1/tasks/{task_id}/{action}")


def test_auto_start_creation_commits_task_mapping_operation_and_audits(client):
    """自动启动创建把任务、成功映射、启动操作和审计作为一个可确认结果返回。"""
    key = uuid4().hex
    body = {
        "name": f"auto-start-{uuid4().hex}", "protocol": "SSH", "ip": "127.0.0.1",
        "port": 22, "resourceId": "fixture-device", "username": "root", "password": "secret",
        "autoStart": True,
    }
    response = client.post("/api/v1/tasks", headers={"Idempotency-Key": key}, json=body)

    assert response.status_code == 201, response.text
    task = response.json()
    repo = client.app.state.repo
    mapping = client.portal.call(repo.db.idempotency.find_one, {"actor": "bootstrap", "key": key})
    operation = client.portal.call(repo.db.operations.find_one, {"id": task["controlOperationId"]})
    async def task_audits():
        return await repo.db.audit.find({"targetId": task["id"]}).to_list(None)

    audits = client.portal.call(task_audits)
    assert mapping["resourceId"] == task["id"] and mapping["state"] == "SUCCEEDED"
    assert (task["desiredState"], task["status"], task["nodeId"]) == ("RUNNING", "STOPPED", None)
    assert (operation["action"], operation["desiredState"], operation["status"]) == (
        "start", "RUNNING", "PENDING",
    )
    assert {entry["action"] for entry in audits} == {"create_task", "control:RUNNING"}
    repeated = client.post("/api/v1/tasks", headers={"Idempotency-Key": key}, json={
        "name": task["name"], "protocol": "SSH", "ip": "127.0.0.1", "port": 22,
        "resourceId": "fixture-device", "username": "root", "password": "secret", "autoStart": True,
    })
    assert repeated.status_code == 201 and repeated.json()["id"] == task["id"]
    assert repeated.json()["operationId"] == task["operationId"]
    stopped = _control(client, task["id"], "stop")
    assert stopped.status_code == 202
    replay_after_control = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": key})
    assert replay_after_control.status_code == 201
    assert replay_after_control.json()["operationId"] == task["operationId"]
    assert replay_after_control.json()["operationId"] != stopped.json()["id"]
    assert client.portal.call(repo.db.tasks.count_documents, {"name": task["name"]}) == 1


def test_running_ssh_pause_stays_pending_until_worker_releases_connection(client):
    """运行中的 SSH 暂停只记录意图；连接未释放前操作不能提前成功。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="COLLECTING", desiredState="RUNNING", nodeId="node-a", runId="run-a")

    response = _control(client, task["id"], "pause")

    assert response.status_code == 202, response.text
    operation = _operation(client, response.json()["id"])
    assert (operation["action"], operation["status"]) == ("pause", "PENDING")
    current = _task(client, task["id"])
    assert current["status"] == "COLLECTING"
    assert current["desiredState"] == "PAUSED"


def test_paused_stop_releases_matching_run_and_preserves_budget(client):
    """暂停后停止释放同一运行的锁并结束运行，但不能清除该运行已占预算。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="PAUSED", desiredState="PAUSED", nodeId=None, runId="paused-run")
    repo = client.app.state.repo
    client.portal.call(repo.db.endpoint_locks.insert_many, [
        {"taskId": task["id"], "runId": "paused-run", "endpoint": "127.0.0.1:22"},
        {"taskId": "other-task", "runId": "other-run", "endpoint": "127.0.0.1:23"},
    ])
    client.portal.call(repo.db.runs.insert_one, {"id": "paused-run"})
    client.portal.call(repo.db.budgets.insert_one, {"_id": "paused-run:periodic", "attempts": 2})

    response = _control(client, task["id"], "stop")

    assert response.status_code == 202, response.text
    assert _task(client, task["id"])["desiredState"] == "STOPPED"
    assert client.portal.call(repo.db.endpoint_locks.find_one, {"taskId": task["id"], "runId": "paused-run"}) is None
    assert client.portal.call(repo.db.endpoint_locks.find_one, {"taskId": "other-task", "runId": "other-run"}) is not None
    assert client.portal.call(repo.db.runs.find_one, {"id": "paused-run"})["endedAt"]
    assert client.portal.call(repo.db.budgets.find_one, {"_id": "paused-run:periodic"})["attempts"] == 2


@pytest.mark.parametrize("task_run_id,lock_run_id", [
    (None, "orphan-lock-run"),
    ("paused-run", "different-lock-run"),
])
def test_paused_stop_rejects_lock_without_matching_task_run(client, task_run_id, lock_run_id):
    """停止不能猜测锁归属：任务无 runId 或锁 runId 不匹配时必须保留原状态。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="PAUSED", desiredState="PAUSED", nodeId=None, runId=task_run_id)
    repo = client.app.state.repo
    client.portal.call(repo.db.endpoint_locks.insert_one,
                       {"taskId": task["id"], "runId": lock_run_id, "endpoint": "127.0.0.1:22"})

    response = _control(client, task["id"], "stop")

    assert response.status_code == 409
    current = _task(client, task["id"])
    assert (current["status"], current["desiredState"], current.get("runId")) == (
        "PAUSED", "PAUSED", task_run_id
    )
    assert client.portal.call(repo.db.endpoint_locks.find_one, {"taskId": task["id"], "runId": lock_run_id})


@pytest.mark.parametrize("missing", ["lock", "run"])
def test_paused_stop_repairs_missing_lock_or_run_then_allows_start(client, missing):
    """暂停记录缺锁或缺运行时，停止可完成可识别清理并允许后续作为新运行启动。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="PAUSED", desiredState="PAUSED", nodeId=None,
              runId="repair-run", sessionId="repair-session")
    repo = client.app.state.repo
    if missing == "lock":
        client.portal.call(repo.db.runs.insert_one, {"id": "repair-run"})
    else:
        client.portal.call(repo.db.endpoint_locks.insert_one,
                           {"taskId": task["id"], "runId": "repair-run", "endpoint": "127.0.0.1:22"})

    stopped = _control(client, task["id"], "stop")

    assert stopped.status_code == 202, stopped.text
    current = _task(client, task["id"])
    assert (current["status"], current["desiredState"], current["nodeId"]) == ("STOPPED", "STOPPED", None)
    assert client.portal.call(repo.db.endpoint_locks.find_one, {"taskId": task["id"]}) is None
    run = client.portal.call(repo.db.runs.find_one, {"id": "repair-run"})
    assert run is None or run.get("endedAt")

    started = _control(client, task["id"], "start")
    assert started.status_code == 202, started.text
    assert _task(client, task["id"])["desiredState"] == "RUNNING"


def test_resume_paused_ssh_keeps_run_budget_and_unassigned_state(client):
    """恢复仅交给调度器重新领取，保留暂停运行、锁和预算且不在 API 建连。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="PAUSED", desiredState="PAUSED", nodeId=None, runId="paused-run")
    repo = client.app.state.repo
    client.portal.call(repo.db.runs.insert_one, {"id": "paused-run"})
    client.portal.call(repo.db.endpoint_locks.insert_one,
                       {"taskId": task["id"], "runId": "paused-run", "endpoint": "127.0.0.1:22"})
    client.portal.call(repo.db.budgets.insert_one, {"_id": "paused-run:periodic", "attempts": 2})

    response = _control(client, task["id"], "resume")

    assert response.status_code == 202, response.text
    operation = _operation(client, response.json()["id"])
    assert (operation["action"], operation["status"]) == ("resume", "PENDING")
    current = _task(client, task["id"])
    assert (current["status"], current["desiredState"], current["nodeId"], current["runId"]) == (
        "PAUSED", "RUNNING", None, "paused-run"
    )
    assert client.portal.call(repo.db.endpoint_locks.count_documents, {"taskId": task["id"], "runId": "paused-run"}) == 1
    assert client.portal.call(repo.db.budgets.find_one, {"_id": "paused-run:periodic"})["attempts"] == 2


def test_telnet_pause_and_resume_are_rejected(client):
    """Telnet 没有可保持的 SSH 运行会话，暂停和恢复均返回冲突。"""
    task = _create_task(client, protocol="TELNET_SERIAL")
    _set_task(client, task["id"], status="PAUSED", desiredState="PAUSED", nodeId=None)

    assert _control(client, task["id"], "pause").status_code == 409
    assert _control(client, task["id"], "resume").status_code == 409


@pytest.mark.parametrize("status,desired,node_id", [
    ("PAUSED", "RUNNING", None),
    ("PAUSED", "PAUSED", "node-a"),
    ("PENDING", "PAUSED", None),
])
def test_resume_requires_paused_status_intent_and_no_owner(client, status, desired, node_id):
    """恢复前必须完成暂停且没有节点归属，任一前置条件缺失均不得写运行意图。"""
    task = _create_task(client)
    _set_task(client, task["id"], status=status, desiredState=desired, nodeId=node_id, runId="paused-run")

    response = _control(client, task["id"], "resume")

    assert response.status_code == 409
    current = _task(client, task["id"])
    assert (current["status"], current["desiredState"], current["nodeId"]) == (status, desired, node_id)


def test_unassigned_pending_task_pauses_immediately_then_resumes_running(client):
    """尚未领取的 SSH 任务暂停时清理已结束运行，恢复时作为新运行重新排队。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="PENDING", desiredState="RUNNING", nodeId=None,
              runId="ended-run", sessionId="ended-session")
    repo = client.app.state.repo
    client.portal.call(repo.db.runs.insert_one, {"id": "ended-run", "endedAt": now()})

    paused = _control(client, task["id"], "pause")

    assert paused.status_code == 202, paused.text
    operation = _operation(client, paused.json()["id"])
    assert (operation["action"], operation["status"]) == ("pause", "SUCCEEDED")
    current = _task(client, task["id"])
    assert (current["status"], current["desiredState"]) == ("PAUSED", "PAUSED")
    assert "runId" not in current and "sessionId" not in current
    resumed = _control(client, task["id"], "resume")
    assert resumed.status_code == 202, resumed.text
    assert _operation(client, resumed.json()["id"])["action"] == "resume"
    assert _task(client, task["id"])["desiredState"] == "RUNNING"


@pytest.mark.parametrize("kind", ["lock", "live_run"])
def test_pending_pause_rejects_any_lock_or_unended_run(client, kind):
    """无节点不代表可安全暂停：残留任务锁或未结束运行必须先由停止流程收尾。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="PENDING", desiredState="RUNNING", nodeId=None,
              runId="unsafe-run", sessionId="unsafe-session")
    repo = client.app.state.repo
    if kind == "lock":
        client.portal.call(repo.db.endpoint_locks.insert_one,
                           {"taskId": task["id"], "runId": "unsafe-run", "endpoint": "127.0.0.1:22"})
    else:
        client.portal.call(repo.db.runs.insert_one, {"id": "unsafe-run"})

    response = _control(client, task["id"], "pause")

    assert response.status_code == 409
    current = _task(client, task["id"])
    assert (current["status"], current["desiredState"], current["runId"], current["sessionId"]) == (
        "PENDING", "RUNNING", "unsafe-run", "unsafe-session"
    )


@pytest.mark.parametrize("action", ["resume", "start"])
@pytest.mark.parametrize("missing", ["lock", "live_run"])
def test_paused_run_requires_live_run_and_matching_lock_before_running(client, action, missing):
    """保留 runId 的暂停任务只能继续可验证的暂停运行，损坏状态须显式停止后重启。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="PAUSED", desiredState="PAUSED", nodeId=None, runId="paused-run")
    repo = client.app.state.repo
    if missing == "lock":
        client.portal.call(repo.db.runs.insert_one, {"id": "paused-run"})
    else:
        client.portal.call(repo.db.endpoint_locks.insert_one,
                           {"taskId": task["id"], "runId": "paused-run", "endpoint": "127.0.0.1:22"})

    response = _control(client, task["id"], action)

    assert response.status_code == 409
    assert _task(client, task["id"])["desiredState"] == "PAUSED"


def test_repeated_resume_reuses_the_accepted_resume_operation(client):
    """同一暂停运行的重复恢复必须返回原 resume 操作，而非因已写 RUNNING 被拒绝或改为 start。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="PAUSED", desiredState="PAUSED", nodeId=None, runId="paused-run")
    repo = client.app.state.repo
    client.portal.call(repo.db.runs.insert_one, {"id": "paused-run"})
    client.portal.call(repo.db.endpoint_locks.insert_one,
                       {"taskId": task["id"], "runId": "paused-run", "endpoint": "127.0.0.1:22"})

    first = _control(client, task["id"], "resume")
    second = _control(client, task["id"], "resume")

    assert first.status_code == second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    operation = _operation(client, first.json()["id"])
    assert (operation["action"], operation["status"]) == ("resume", "PENDING")


@pytest.mark.parametrize("status,desired,action", [
    ("STOPPED", "STOPPED", "stop"),
    ("COLLECTING", "RUNNING", "start"),
])
def test_same_terminal_intent_reuses_existing_succeeded_operation(client, status, desired, action):
    """连续同状态请求即使操作已成功也复用同一操作，避免制造无意义的轮询对象。"""
    task = _create_task(client)
    changes = {"status": status, "desiredState": desired, "nodeId": None}
    if action == "start":
        changes.update(nodeId="node-a", runId="running-run", generation=1)
        repo = client.app.state.repo
        client.portal.call(repo.db.nodes.insert_one,
                           {"id": "node-a", "heartbeat": now(), "accepting": True, "capacity": 1})
        client.portal.call(repo.db.runs.insert_one, {"id": "running-run"})
        client.portal.call(repo.db.endpoint_locks.insert_one,
                           {"taskId": task["id"], "runId": "running-run", "endpoint": "127.0.0.1:22"})
    _set_task(client, task["id"], **changes)

    first = _control(client, task["id"], action)
    second = _control(client, task["id"], action)

    assert first.status_code == second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    operation = _operation(client, first.json()["id"])
    assert (operation["action"], operation["status"]) == (action, "SUCCEEDED")


def test_reverse_control_cancels_pending_operation_without_claiming_its_terminal_state(client):
    """反向意图取消旧 PENDING 操作；旧操作不得被伪造为暂停或停止成功。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="COLLECTING", desiredState="RUNNING", nodeId="node-a", runId="run-a")
    pausing = _control(client, task["id"], "pause")
    assert _operation(client, pausing.json()["id"])["status"] == "PENDING"

    stopping = _control(client, task["id"], "stop")

    assert stopping.status_code == 202, stopping.text
    assert _operation(client, pausing.json()["id"])["status"] == "CANCELLED"
    assert _task(client, task["id"])["status"] == "COLLECTING"


def test_deleted_resource_rejects_start_pause_resume_but_allows_stop(client):
    """删除资源后禁止任何重启或暂停恢复意图，但停止仍可清理遗留运行。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="PAUSED", desiredState="PAUSED", nodeId=None, resourceDeleted=True)
    client.portal.call(client.app.state.repo.db.resources.update_one,
                       {"id": "fixture-device"}, {"$set": {"deletedAt": "deleted"}})

    for action in ("start", "pause", "resume"):
        assert _control(client, task["id"], action).status_code == 409
    assert _control(client, task["id"], "stop").status_code == 202


def test_unassigned_error_task_start_returns_to_scheduler_queue(client):
    """无节点归属的异常任务重启会清除错误并回到待调度的停止状态。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="ERROR", desiredState="STOPPED", nodeId=None, error="connection failed")

    response = _control(client, task["id"], "start")

    assert response.status_code == 202, response.text
    current = _task(client, task["id"])
    assert (current["status"], current["desiredState"], current["nodeId"], current["error"]) == (
        "STOPPED", "RUNNING", None, None
    )
