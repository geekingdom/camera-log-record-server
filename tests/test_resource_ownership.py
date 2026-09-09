"""共享读取与创建者写入隔离；地址占用、软删除和跨用户关联回归。"""
# ruff: noqa: F811
from camera_logs.common.security import actor
from test_api import client  # noqa: F401

SCOPES = ["tasks:read", "tasks:create", "tasks:write", "tasks:control", "resources:create",
          "resources:write", "logs:read", "logs:download", "commands:send"]


def as_user(client, identifier):
    """仅替换认证身份以聚焦对象权限，登录/令牌动态继承另由用户测试覆盖。"""
    identity = {"id": identifier, "displayName": identifier, "isAdmin": False, "scopes": SCOPES}
    client.app.dependency_overrides[actor] = lambda: identity


def resource(client, ip="192.0.2.71", key="resource"):
    return client.post("/api/v1/resources", json={"name": "共享串口资源", "kind": "SERIAL_SERVER", "ip": ip},
                       headers={"Idempotency-Key": key})


def task(client, resource_id, key="task"):
    response = client.post("/api/v1/tasks", headers={"Idempotency-Key": key}, json={
        "name": "独立归属任务", "resourceId": resource_id, "protocol": "TELNET_SERIAL",
        "ip": "192.0.2.71", "port": 10002,
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_resource_address_unique_and_soft_delete_releases_address(client):
    as_user(client, "alice")
    first = resource(client)
    assert first.status_code == 201, first.text
    created = first.json()
    assert created["createdBy"] == "alice" and created["createdByName"] == "alice"
    assert resource(client, key="duplicate").status_code == 409
    assert resource(client).json()["id"] == created["id"]
    assert client.delete(f'/api/v1/resources/{created["id"]}?version=1').status_code == 202
    second = resource(client, key="recreate")
    assert second.status_code == 201 and second.json()["id"] != created["id"]
    assert client.get(f'/api/v1/resources/{created["id"]}').json()["deletedAt"]


def test_other_users_can_read_but_not_mutate_and_owner_is_not_client_controlled(client):
    as_user(client, "alice")
    created = resource(client).json()
    own_task = task(client, created["id"])
    assert own_task["createdBy"] == "alice"
    as_user(client, "bob")
    assert client.get(f'/api/v1/resources/{created["id"]}').status_code == 200
    assert client.get(f'/api/v1/tasks/{own_task["id"]}').status_code == 200
    assert client.patch(f'/api/v1/tasks/{own_task["id"]}', json={"version": 1, "name": "不允许"}).status_code == 403
    for action in ("start", "stop", "pause", "resume"):
        assert client.post(f'/api/v1/tasks/{own_task["id"]}/{action}').status_code == 403
    assert client.post(f'/api/v1/tasks/{own_task["id"]}/commands', json={"command": "ls"},
                       headers={"Idempotency-Key": "foreign-command"}).status_code == 403
    body = {"version": 1, "name": "不允许", "kind": "SERIAL_SERVER", "ip": "192.0.2.71"}
    assert client.patch(f'/api/v1/resources/{created["id"]}', json=body).status_code == 403
    assert client.delete(f'/api/v1/resources/{created["id"]}?version=1').status_code == 403
    assert client.patch(f'/api/v1/tasks/{own_task["id"]}', json={"version": 1, "createdBy": "bob"}).status_code == 422
    # 任务归属独立于资源归属：可以在共享资源下创建自己的采集任务。
    other_task = task(client, created["id"])
    assert other_task["createdBy"] == "bob"
    as_user(client, "alice")
    assert client.delete(f'/api/v1/resources/{created["id"]}?version=1').status_code == 403
    client.app.dependency_overrides.clear()
    assert client.post(f'/api/v1/tasks/{own_task["id"]}/stop').status_code == 202
    assert client.delete(f'/api/v1/resources/{created["id"]}?version=1').status_code == 202
