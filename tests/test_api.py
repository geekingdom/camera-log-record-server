"""正式 API 回归：任务幂等创建、敏感字段隔离、模板快照和启停契约。"""
import pytest
from camera_logs.common.config import Settings
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient


@pytest.fixture
def client(tmp_path):
    settings = Settings(bootstrap_token="test-admin-token", encryption_key=Fernet.generate_key().decode(),
                        log_root=tmp_path, node_id="test", start_background=False)
    with TestClient(create_app(settings, AsyncMongoMockClient().camera_logs)) as client:
        client.headers["Authorization"] = "Bearer test-admin-token"
        client.portal.call(client.app.state.repo.db.resources.insert_one, {
            "id": "fixture-device", "name": "测试设备", "kind": "HIKVISION_NETWORK",
            "ip": "127.0.0.1", "model": "test-model", "subSerialNumber": "test-serial",
            "authenticatedAt": "2026-09-08T00:00:00Z"})
        yield client


@pytest.fixture
def awaitable_mongomock_event_aggregate(client, monkeypatch):
    """将模拟器聚合游标包装为生产 PyMongo 异步接口所需的可等待返回值。"""
    collection_type = type(client.app.state.repo.db.events)
    aggregate = collection_type.aggregate

    async def awaitable_aggregate(self, *args, **kwargs):
        """测试替身只修复调用接口，聚合管道继续由 mongomock 执行。"""
        return aggregate(self, *args, **kwargs)

    monkeypatch.setattr(collection_type, "aggregate", awaitable_aggregate)


def test_task_idempotency_and_password_secrecy(client):
    body = {"name": "camera", "protocol": "SSH", "ip": "127.0.0.1", "port": 22, "username": "root",
            "password": " secret ", "resourceId": "fixture-device"}
    headers = {"Idempotency-Key": "create-task-1"}
    result = client.post("/api/v1/tasks", json=body, headers=headers)
    assert result.status_code == 201, result.text
    task = result.json()
    assert "password" not in task and "passwordEncrypted" not in task
    assert task["hasPassword"]
    repeated = client.post("/api/v1/tasks", json=body, headers=headers)
    assert repeated.json()["id"] == task["id"]
    assert client.post("/api/v1/tasks", json=body | {"name": "other"}, headers=headers).status_code == 409
    changed = client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "name": "changed", "password": ""})
    assert changed.status_code == 200, changed.text
    assert changed.json()["hasPassword"]
    assert client.patch(f'/api/v1/tasks/{task["id"]}', json={"version": 1, "name": "stale"}).status_code == 409


def test_template_snapshot_and_separate_command_ids(client):
    template = client.post("/api/v1/command-templates", headers={"Idempotency-Key": "tpl"},
        json={"name": "基础", "initialCommands": [{"command": "date"}],
              "scheduledCommands": [{"command": "uptime", "totalExecutions": 2, "intervalSeconds": 1}]}).json()
    tasks = [client.post("/api/v1/tasks", headers={"Idempotency-Key": str(i)}, json={
        "name": str(i), "protocol": "TELNET_SERIAL", "ip": "127.0.0.1", "port": 9000+i,
        "resourceId": "fixture-device",
        "sourceTemplateId": template["id"], "sourceTemplateVersion": template["version"],
        "initialCommands": template["initialCommands"], "scheduledCommands": template["scheduledCommands"]}).json() for i in range(2)]
    assert tasks[0]["scheduledCommands"][0]["id"] != tasks[1]["scheduledCommands"][0]["id"]
    assert client.delete(f'/api/v1/command-templates/{template["id"]}?version=1').status_code == 204
    assert client.get(f'/api/v1/tasks/{tasks[0]["id"]}').json()["initialCommands"][0]["command"] == "date"


def test_auth_and_invalid_commands(client):
    assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/v1/tasks", headers={"Idempotency-Key": "bad"},
        json={"name": "bad", "protocol": "SSH", "ip": "x", "port": 0}).status_code == 422


def test_stop_idempotence_and_start_endpoints(client):
    task = client.post("/api/v1/tasks", headers={"Idempotency-Key": "serial"},
        json={"name": "serial", "protocol": "TELNET_SERIAL", "ip": "127.0.0.1", "port": 9900,
              "resourceId": "fixture-device"}).json()
    for _ in range(2):
        result = client.post(f'/api/v1/tasks/{task["id"]}/stop')
        assert result.status_code == 202
        assert client.get(f'/api/v1/operations/{result.json()["id"]}').json()["status"] == "SUCCEEDED"
