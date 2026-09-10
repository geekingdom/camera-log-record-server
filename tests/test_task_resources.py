"""资源归属必须在任务 API 边界校验，每个任务均使用统一资源结构。"""
# ruff: noqa: F811 - pytest 按名称注入复用的 client fixture。

from uuid import uuid4

import pytest
from test_api import client  # noqa: F401


def seed_resource(client, identifier="network", **changes):
    """注入已由资源认证模块验证的资源，隔离设备网络依赖。"""
    resource = {"id": identifier, "kind": "HIKVISION_NETWORK", "ip": "127.0.0.1",
                "name": identifier, "model": "camera/model", "subSerialNumber": "SN001",
                "authenticatedAt": "2026-09-08T00:00:00Z"} | changes
    client.portal.call(client.app.state.repo.db.resources.insert_one, resource)
    return resource


def create(client, **changes):
    body = {"name": "capture", "protocol": "SSH", "ip": "127.0.0.1", "port": 22,
            "username": "root", "password": "secret", "resourceId": "network"} | changes
    return client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": uuid4().hex})


def test_create_requires_real_authenticated_resource(client):
    assert create(client, resourceId=None).status_code == 422
    assert create(client).status_code == 404
    seed_resource(client, authenticatedAt=None)
    assert create(client).status_code == 409


@pytest.mark.parametrize("protocol", ["SSH", "TELNET_DEVICE"])
def test_network_ip_locked_but_duplicate_port_allowed(client, protocol):
    seed_resource(client)
    assert create(client, protocol=protocol, ip="127.0.0.2").status_code == 422
    first = create(client, protocol=protocol)
    second = create(client, protocol=protocol)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["storageIdentity"] == second.json()["storageIdentity"]
    assert client.get("/api/v1/tasks?resourceId=network").json()["total"] == 2
    assert client.get("/api/v1/tasks?resourceId=other").json()["total"] == 0
    changed = client.patch(f'/api/v1/tasks/{first.json()["id"]}', json={"version": 1, "ip": "127.0.0.2"})
    assert changed.status_code == 422


def test_serial_resource_only_serial_protocol_and_own_ip(client):
    seed_resource(client, "serial", kind="SERIAL_SERVER", ip="10.0.0.8")
    assert create(client, resourceId="serial", ip="10.0.0.8").status_code == 422
    assert create(client, resourceId="serial", protocol="TELNET_SERIAL").status_code == 422
    assert create(client, resourceId="serial", protocol="TELNET_SERIAL", ip="10.0.0.8").status_code == 201


def test_network_serial_target_supports_selection_and_custom(client):
    seed_resource(client)
    seed_resource(client, "serial", kind="SERIAL_SERVER", ip="10.0.0.8")
    assert create(client, protocol="TELNET_SERIAL", ip="10.0.0.9").status_code == 201
    assert create(client, protocol="TELNET_SERIAL", ip="10.0.0.8", serialServerResourceId="serial").status_code == 201
    assert client.get("/api/v1/tasks?resourceId=serial").json()["total"] == 1
    assert create(client, protocol="TELNET_SERIAL", ip="10.0.0.9", serialServerResourceId="serial").status_code == 422
    assert create(client, protocol="TELNET_SERIAL", serialServerResourceId="network").status_code == 422
    assert create(client, serialServerResourceId="serial").status_code == 422


def test_same_physical_device_shares_storage_independent_of_resource_name(client):
    seed_resource(client)
    seed_resource(client, "duplicate")
    seed_resource(client, "replacement", subSerialNumber="SN002")
    first = create(client).json()
    same = create(client, resourceId="duplicate").json()
    different = create(client, resourceId="replacement").json()
    assert first["storageIdentity"] == same["storageIdentity"]
    assert first["storageIdentity"] != different["storageIdentity"]


def test_task_resource_identity_cannot_be_reassigned_or_forged(client):
    seed_resource(client)
    task = create(client).json()
    assert create(client, storageIdentity="forged").status_code == 422
    assert client.patch(f'/api/v1/tasks/{task["id"]}',
                        json={"version": 1, "resourceId": "other"}).status_code == 422


@pytest.mark.parametrize("metadata", [{}, {"model": None, "subSerialNumber": None},
                                     {"model": "", "subSerialNumber": ""}])
def test_empty_device_identity_can_create_task(client, metadata):
    """设备身份可缺失，任务仍可创建且文件夹不含 Python 空值文本。"""
    resource = seed_resource(client)
    client.portal.call(client.app.state.repo.db.resources.replace_one, {"id": "network"},
                       {key: value for key, value in resource.items() if key not in {"model", "subSerialNumber"}} | metadata)
    response = create(client)
    assert response.status_code == 201
    identity = response.json()["storageIdentity"]
    assert identity.startswith("127.0.0.1-unknown-unknown-")
    assert "None" not in identity


def test_long_device_identity_keeps_discriminator_after_storage_sanitizing(client):
    """两个长型号共享前缀时，存储层截断不能丢失最终身份区分信息。"""
    from camera_logs.logs.naming import safe_filename_component
    seed_resource(client, model="x" * 512, subSerialNumber="y" * 512)
    seed_resource(client, "other", model="x" * 512, subSerialNumber="y" * 511 + "z")
    first = create(client).json()["storageIdentity"]
    second = create(client, resourceId="other").json()["storageIdentity"]
    assert len(first.encode()) <= 80
    assert safe_filename_component(first) != safe_filename_component(second)


def test_soft_deleted_resource_blocks_new_tasks_and_restart_but_preserves_logs(client, tmp_path):
    seed_resource(client, version=1)
    task = create(client).json()
    path = tmp_path / "kept.log"
    path.write_bytes(b"preserved device output\n")
    client.portal.call(client.app.state.repo.db.files.insert_one, {
        "id": "retained-file", "taskId": task["id"], "hour": "2026-09-08T00:00:00+00:00",
        "path": str(path), "status": "READY", "bytes": path.stat().st_size})
    result = client.delete("/api/v1/resources/network?version=1")
    assert result.status_code == 202
    assert path.read_bytes() == b"preserved device output\n"
    assert client.get(f'/api/v1/tasks/{task["id"]}/log-hours').json()["total"] == 1
    assert create(client).status_code == 409
    assert client.post(f'/api/v1/tasks/{task["id"]}/start').status_code == 409
    assert client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "name": "changed"}).status_code == 409


def test_delete_wins_against_late_start_request(client, monkeypatch):
    """启动已读过资源后，删除写入的标记仍阻止迟到的 RUNNING 更新。"""
    seed_resource(client, version=1)
    task = create(client).json()
    collection = client.app.state.repo.db.tasks
    collection_type = type(collection)
    original = collection_type.update_one

    async def delete_before_start(self, query, update, **kwargs):
        if self.name == "tasks" and update.get("$set", {}).get("desiredState") == "RUNNING":
            await original(self, {"id": task["id"]},
                           {"$set": {"resourceDeleted": True, "desiredState": "STOPPED"}})
        return await original(self, query, update, **kwargs)

    monkeypatch.setattr(collection_type, "update_one", delete_before_start)
    assert client.post(f'/api/v1/tasks/{task["id"]}/start').status_code == 409
    assert client.get(f'/api/v1/tasks/{task["id"]}').json()["desiredState"] == "STOPPED"


def test_delete_wins_against_late_edit_request(client, monkeypatch):
    """编辑已校验资源后，删除标记禁止写回可重启的新配置。"""
    seed_resource(client, version=1)
    task = create(client).json()
    collection = client.app.state.repo.db.tasks
    collection_type = type(collection)
    original = collection_type.find_one_and_update

    async def delete_before_edit(self, query, update, **kwargs):
        if self.name == "tasks":
            await self.update_one({"id": task["id"]}, {"$set": {"resourceDeleted": True}})
        return await original(self, query, update, **kwargs)

    monkeypatch.setattr(collection_type, "find_one_and_update", delete_before_edit)
    assert client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "name": "late"}).status_code == 409
    assert client.get(f'/api/v1/tasks/{task["id"]}').json()["name"] == "capture"
