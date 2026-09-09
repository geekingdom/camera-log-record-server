"""任务编辑的受控停止、操作和审计事务契约。"""
# ruff: noqa: F811 - pytest 通过导入名称复用 test_api 的 client fixture。

from uuid import uuid4

from camera_logs.common.database import now
from camera_logs.tasks.scheduler import schedule_once
from test_api import client  # noqa: F401


def _task(client):
    """创建一项带认证信息的任务，供正式 PATCH 路由编辑。"""
    response = client.post("/api/v1/tasks", headers={"Idempotency-Key": uuid4().hex}, json={
        "name": "edit-" + uuid4().hex, "protocol": "SSH", "ip": "127.0.0.1", "port": 22,
        "resourceId": "fixture-device", "username": "root", "password": "secret",
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_running_behavioral_edit_records_stop_operation_cancels_opposite_and_audits(client):
    """运行任务改连接配置必须原子写入受控停止、取消旧启动和编辑审计。"""
    task = _task(client)
    client.portal.call(client.app.state.repo.db.tasks.update_one, {"id": task["id"]}, {
        "$set": {"desiredState": "RUNNING", "status": "COLLECTING", "nodeId": "worker-a"},
    })
    client.portal.call(client.app.state.repo.db.operations.insert_one, {
        "id": "old-start", "taskId": task["id"], "desiredState": "RUNNING", "status": "PENDING",
    })

    edited = client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "port": 23})

    assert edited.status_code == 200, edited.text
    current = edited.json()
    assert current["port"] == 23
    assert current["desiredState"] == "STOPPED" and current["restartRequested"] is True
    operation = client.portal.call(client.app.state.repo.db.operations.find_one, {"id": current["controlOperationId"]})
    old = client.portal.call(client.app.state.repo.db.operations.find_one, {"id": "old-start"})
    audit = client.portal.call(client.app.state.repo.db.audit.find_one, {"targetId": task["id"], "action": "edit_task"})
    assert (operation["action"], operation["desiredState"], operation["status"]) == ("edit-stop", "STOPPED", "PENDING")
    assert old["status"] == "CANCELLED"
    assert audit is not None


def test_queued_behavioral_edit_stays_schedulable(client, mock_claim_transaction):
    """无 Worker 的排队任务修改连接配置后仍保持运行意图，下一周期可以被领取。"""
    task = _task(client)
    database = client.app.state.repo.db
    client.portal.call(database.tasks.update_one, {"id": task["id"]}, {"$set": {
        "desiredState": "RUNNING", "status": "STOPPED", "nodeId": None,
        "generation": 0, "resourceDeleted": False,
    }})
    client.portal.call(database.nodes.insert_one, {
        "id": "node-a", "heartbeat": now(), "diskPercent": 10,
        "accepting": True, "capacity": 100,
    })

    edited = client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "port": 23})

    assert edited.status_code == 200, edited.text
    current = edited.json()
    assert current["desiredState"] == "RUNNING" and current["restartRequested"] is False
    assert client.portal.call(database.operations.count_documents, {
        "taskId": task["id"], "action": "edit-stop",
    }) == 0
    client.portal.call(schedule_once, client.app.state.repo)
    claimed = client.portal.call(database.tasks.find_one, {"id": task["id"]})
    assert claimed["nodeId"] == "node-a"
    assert claimed["status"] == "PENDING" and claimed["desiredState"] == "RUNNING"


def test_queued_behavioral_edit_rejects_unconfirmed_old_run(client):
    """无归属不等于没有旧连接：运行锁、活动运行或缺失运行都不能覆盖配置。"""
    task = _task(client)
    database = client.app.state.repo.db
    client.portal.call(database.tasks.update_one, {"id": task["id"]}, {"$set": {
        "desiredState": "RUNNING", "status": "STOPPED", "nodeId": None, "runId": "old-run",
    }})
    client.portal.call(database.endpoint_locks.insert_one, {
        "taskId": task["id"], "runId": "old-run", "endpoint": "127.0.0.1:22",
    })

    locked = client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "port": 23})

    assert locked.status_code == 409
    assert client.portal.call(database.tasks.find_one, {"id": task["id"]})["port"] == 22
    client.portal.call(database.endpoint_locks.delete_one, {"taskId": task["id"]})
    unknown = client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "port": 23})
    assert unknown.status_code == 409
    client.portal.call(database.runs.insert_one, {"id": "old-run", "taskId": task["id"]})
    active = client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "port": 23})
    assert active.status_code == 409
    assert client.portal.call(database.tasks.find_one, {"id": task["id"]})["port"] == 22


def test_edit_rejects_stale_preparation_and_paused_transition_before_commit(client, monkeypatch):
    """准备快照不能跨版本复用，事务内状态变为暂停后也不能写入行为配置。"""
    task = _task(client)
    assert client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 2, "name": "future"}).status_code == 409

    from camera_logs.tasks import editing
    original = editing._prepared

    async def pause_after_prepare(repo, user, task_id, body):
        result = await original(repo, user, task_id, body)
        await repo.db.tasks.update_one({"id": task_id}, {"$set": {"status": "COLLECTING", "desiredState": "PAUSED"}})
        return result

    monkeypatch.setattr(editing, "_prepared", pause_after_prepare)
    response = client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "port": 23})
    assert response.status_code == 409
    stored = client.portal.call(client.app.state.repo.db.tasks.find_one, {"id": task["id"]})
    assert stored["port"] == 22 and stored["desiredState"] == "PAUSED"


def test_edit_rejects_resource_soft_delete_after_preparation(client, monkeypatch):
    """资源在预处理后软删除时，声明写 guard 必须阻止任务配置提交。"""
    task = _task(client)
    from camera_logs.tasks import editing
    original = editing._prepared

    async def delete_after_prepare(repo, user, task_id, body):
        result = await original(repo, user, task_id, body)
        await repo.db.resources.update_one({"id": "fixture-device"}, {"$set": {"deletedAt": "now"}})
        return result

    monkeypatch.setattr(editing, "_prepared", delete_after_prepare)
    response = client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "port": 23})
    assert response.status_code == 409
    stored = client.portal.call(client.app.state.repo.db.tasks.find_one, {"id": task["id"]})
    assert stored["port"] == 22


def test_edit_rejects_resource_identity_change_after_preparation(client, monkeypatch):
    """预处理绑定的设备身份变化时，不得以旧存储身份覆盖更新后的资源。"""
    task = _task(client)
    from camera_logs.tasks import editing
    original = editing._prepared

    async def replace_identity_after_prepare(repo, user, task_id, body):
        result = await original(repo, user, task_id, body)
        await repo.db.resources.update_one({"id": "fixture-device"}, {"$set": {"subSerialNumber": "new-serial"}})
        return result

    monkeypatch.setattr(editing, "_prepared", replace_identity_after_prepare)
    response = client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "port": 23})
    assert response.status_code == 409
    stored = client.portal.call(client.app.state.repo.db.tasks.find_one, {"id": task["id"]})
    assert stored["port"] == 22


def test_edit_rejects_identity_change_between_snapshot_and_binding(client, monkeypatch):
    """资源在快照后、绑定前变更时，不能混用旧快照与新存储身份提交。"""
    task = _task(client)
    from camera_logs.tasks import editing
    original = editing.bind_resource

    async def change_before_binding(repo, checked):
        await client.app.state.repo.db.resources.update_one(
            {"id": "fixture-device"}, {"$set": {"subSerialNumber": "newer-serial"}}
        )
        return await original(repo, checked)

    monkeypatch.setattr(editing, "bind_resource", change_before_binding)
    response = client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "port": 23})
    assert response.status_code == 409
    stored = client.portal.call(client.app.state.repo.db.tasks.find_one, {"id": task["id"]})
    assert stored["port"] == 22
