"""验证子账户资源范围在任务、日志、命令和下载入口均由后端强制执行。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from camera_logs.common.config import Settings
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

CSRF = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def client(tmp_path):
    """使用独立内存库构造资源范围案例，不访问真实海康设备或采集节点。"""
    settings = Settings(_env_file=None, bootstrap_token="permissions-bootstrap",
                        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                        admin_password="permissions-admin-password", start_background=False)
    with TestClient(create_app(settings, AsyncMongoMockClient().camera_logs)) as result:
        yield result


def seed_resource(client, identifier, *, serial=False, ip="192.0.2.10"):
    """写入经认证的测试资源，使任务绑定只验证本地持久化状态。"""
    resource = {"id": identifier, "name": identifier, "kind": "SERIAL_SERVER" if serial else "HIKVISION_NETWORK",
                "ip": ip, "deletedAt": None, "version": 1}
    if not serial:
        resource.update(model="DS-2CD", subSerialNumber=identifier, authenticatedAt=datetime.now(UTC))
    client.portal.call(client.app.state.repo.db.resources.insert_one, resource)


def seed_task(client, identifier, resource_id, *, serial_id=None, ip="192.0.2.10"):
    """写入可用于全部二级对象授权的任务快照。"""
    client.portal.call(client.app.state.repo.db.tasks.insert_one, {
        "id": identifier, "name": identifier, "resourceId": resource_id, "serialServerResourceId": serial_id,
        "ip": ip, "protocol": "TELNET_SERIAL" if serial_id else "SSH", "port": 23 if serial_id else 22,
        "username": "root", "passwordEncrypted": "", "hasPassword": False,
        "desiredState": "STOPPED", "status": "STOPPED", "nodeId": None, "version": 1,
        "initialCommands": [], "scheduledCommands": [], "description": "", "encoding": "utf-8",
        "loginPrompt": "login:", "passwordPrompt": "Password:", "createdAt": datetime.now(UTC),
        "updatedAt": datetime.now(UTC), "generation": 0,
    })


def operator_session(client):
    """用管理员 Bearer 创建普通账户，再改密进入可操作的 cookie 会话。"""
    client.headers["Authorization"] = "Bearer permissions-bootstrap"
    created = client.post("/api/v1/users", json={
        "username": "operator", "displayName": "操作员", "password": "operator-password",
        "scopes": ["tasks:write", "tasks:create", "tasks:control", "resources:create",
                   "resources:write", "commands:send"],
    })
    assert created.status_code == 201, created.text
    del client.headers["Authorization"]
    assert client.post("/api/v1/auth/login", json={"username": "operator", "password": "operator-password"}, headers=CSRF).status_code == 200
    changed = client.post("/api/v1/auth/password", json={
        "currentPassword": "operator-password", "newPassword": "operator-password-next",
    }, headers=CSRF)
    assert changed.status_code == 200, changed.text


def test_ordinary_user_reads_all_task_derived_data_but_cannot_manage_accounts(client):
    """普通有效用户自动拥有全量读取和下载权限，用户管理仍只允许管理员。"""
    seed_resource(client, "allowed")
    seed_resource(client, "denied")
    seed_resource(client, "serial-denied", serial=True, ip="192.0.2.20")
    seed_task(client, "denied-task", "denied")
    seed_task(client, "mixed-task", "allowed", serial_id="serial-denied", ip="192.0.2.20")
    repo = client.app.state.repo
    client.portal.call(repo.db.commands.insert_one, {"id": "denied-command", "taskId": "denied-task", "status": "QUEUED"})
    client.portal.call(repo.db.operations.insert_one, {"id": "denied-operation", "taskId": "denied-task", "status": "PENDING"})
    client.portal.call(repo.db.files.insert_one, {"id": "denied-file", "taskId": "denied-task", "nodeId": "node", "status": "READY", "hour": "2026-09-09T00:00:00+00:00"})
    client.portal.call(repo.db.jobs.insert_one, {"id": "denied-download", "taskId": "denied-task", "kind": "DOWNLOAD", "status": "SUCCEEDED", "actor": "other"})
    client.portal.call(repo.db.jobs.insert_one, {"id": "denied-search", "taskId": "denied-task", "kind": "SEARCH", "status": "SUCCEEDED", "actor": "other", "results": []})
    operator_session(client)

    assert client.get("/api/v1/resources/denied").status_code == 200
    assert client.get("/api/v1/resources/allowed").status_code == 200
    tasks = client.get("/api/v1/tasks")
    assert tasks.status_code == 200 and tasks.json()["total"] == 2
    for path in (
        "/api/v1/tasks/denied-task", "/api/v1/tasks/mixed-task",
        "/api/v1/operations/denied-operation", "/api/v1/commands/denied-command",
        "/api/v1/tasks/denied-task/command-executions", "/api/v1/tasks/denied-task/log-hours",
        "/api/v1/downloads/denied-download",
        "/api/v1/log-searches/denied-search", "/api/v1/log-searches/denied-search/results",
    ):
        assert client.get(path).status_code == 200
    assert client.get("/api/v1/users").status_code == 403
    assert client.post("/api/v1/users", json={
        "username": "forbidden", "displayName": "禁止", "password": "forbidden-password",
    }, headers=CSRF).status_code == 403


def test_ordinary_user_keeps_configured_write_scopes_without_resource_range(client):
    """资源范围已取消，普通用户仍必须具备对应写入 scope。"""
    seed_resource(client, "allowed")
    operator_session(client)

    created = client.post("/api/v1/tasks", json={
        "name": "new allowed task", "description": "", "protocol": "SSH", "ip": "192.0.2.10", "port": 22,
        "username": "root", "password": "device-password", "resourceId": "allowed",
    }, headers=CSRF | {"Idempotency-Key": uuid4().hex})
    assert created.status_code == 201, created.text
    changed = client.patch(f"/api/v1/tasks/{created.json()['id']}", json={
        "version": created.json()["version"], "name": "renamed own task",
    }, headers=CSRF)
    assert changed.status_code == 200
