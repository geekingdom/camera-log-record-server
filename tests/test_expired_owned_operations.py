"""验证额外权限到期后，本人资源和任务仍可编辑删除且不越过对象或来源边界。"""
# ruff: noqa: F811

from camera_logs.common.security import actor
from test_api import client  # noqa: F401

FULL_SCOPES = ["tasks:read", "tasks:create", "tasks:write", "tasks:control", "resources:create", "resources:write"]


def as_identity(client, identifier, *, expired=False, ip_scopes=None):
    """替换当前请求身份；资源和任务写入仍经真实路由与对象鉴权。"""
    identity = {"id": identifier, "displayName": identifier, "isAdmin": False,
                "scopes": ["commands:send"] if expired else FULL_SCOPES}
    if expired:
        identity["permissionsExpired"] = True
    if ip_scopes is not None:
        identity["ipAllowedScopes"] = ip_scopes

    async def seed():
        await client.app.state.repo.db.users.update_one({"id": identifier}, {"$set": {
            "id": identifier, "username": identifier, "displayName": identifier,
            "enabled": True, "deletedAt": None,
        }}, upsert=True)

    client.portal.call(seed)
    client.app.dependency_overrides[actor] = lambda: identity


def create_resource(client, name, ip):
    """创建串口资源，避免测试例外时产生真实设备认证请求。"""
    response = client.post("/api/v1/resources", json={
        "name": name, "kind": "SERIAL_SERVER", "ip": ip,
    }, headers={"Idempotency-Key": f"resource-{name}"})
    assert response.status_code == 201, response.text
    return response.json()


def create_task(client, resource, name):
    """创建绑定串口资源的任务，保留真实任务写入与归属字段。"""
    response = client.post("/api/v1/tasks", json={
        "name": name, "resourceId": resource["id"], "protocol": "TELNET_SERIAL",
        "ip": resource["ip"], "port": 10002,
    }, headers={"Idempotency-Key": f"task-{name}"})
    assert response.status_code == 201, response.text
    return response.json()


def test_expired_user_only_mutates_own_resources_and_tasks(client):
    """到期身份没有写入或控制 scope，仍可收束本人配置且不能操作他人对象。"""
    as_identity(client, "alice")
    own_resource = create_resource(client, "alice-resource", "192.0.2.201")
    own_task = create_task(client, own_resource, "alice-task")
    delete_resource = create_resource(client, "alice-delete", "192.0.2.202")
    as_identity(client, "bob")
    foreign_resource = create_resource(client, "bob-resource", "192.0.2.203")
    foreign_task = create_task(client, foreign_resource, "bob-task")

    as_identity(client, "alice", expired=True, ip_scopes=["resources:write", "tasks:write", "tasks:control"])

    changed_resource = client.patch(f'/api/v1/resources/{own_resource["id"]}', json={
        "version": 1, "name": "alice-resource-edited", "kind": "SERIAL_SERVER", "ip": own_resource["ip"],
    })
    assert changed_resource.status_code == 200, changed_resource.text
    changed_task = client.patch(f'/api/v1/tasks/{own_task["id"]}', json={"version": 1, "name": "alice-task-edited"})
    assert changed_task.status_code == 200, changed_task.text
    assert client.delete(f'/api/v1/tasks/{own_task["id"]}?version=2').status_code == 202
    assert client.delete(f'/api/v1/resources/{delete_resource["id"]}?version=1').status_code == 202

    foreign_patch = client.patch(f'/api/v1/resources/{foreign_resource["id"]}', json={
        "version": 1, "name": "forbidden", "kind": "SERIAL_SERVER", "ip": foreign_resource["ip"],
    })
    assert foreign_patch.status_code == 403
    assert client.delete(f'/api/v1/tasks/{foreign_task["id"]}?version=1').status_code == 403


def test_expired_owned_exception_remains_subject_to_source_ip_scopes(client):
    """即使对象为本人创建，来源策略遗漏写入或收尾作用域时也不能绕过限制。"""
    as_identity(client, "alice")
    resource = create_resource(client, "ip-limited-resource", "192.0.2.204")
    task = create_task(client, resource, "ip-limited-task")

    as_identity(client, "alice", expired=True, ip_scopes=["tasks:read", "tasks:write"])
    denied_resource = client.patch(f'/api/v1/resources/{resource["id"]}', json={
        "version": 1, "name": "forbidden", "kind": "SERIAL_SERVER", "ip": resource["ip"],
    })
    assert denied_resource.status_code == 403
    assert client.delete(f'/api/v1/tasks/{task["id"]}?version=1').status_code == 403
