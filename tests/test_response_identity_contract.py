"""真实账号、命令和控制路由返回必须有完整字段说明。"""

import pytest
from test_api import client as _api_client
from test_response_admin_contract import assert_documented
from test_user_auth import ADMIN_PASSWORD, CSRF, login
from test_user_auth import client as _user_client


@pytest.fixture(name="client")
def client_fixture(tmp_path):
    """建立带资源的管理 API。"""
    yield from _api_client.__wrapped__(tmp_path)


@pytest.fixture(name="user_client")
def user_client_fixture(tmp_path):
    """建立真实 Cookie 登录 API。"""
    yield from _user_client.__wrapped__(tmp_path)


def assert_response(client, identifier, response):
    """按实际目录合同检查成功正文及全部嵌套字段。"""
    assert response.is_success, response.text
    catalog = client.get("/api/v1/api-reference").json()
    operation = next(item for item in catalog["operations"] if item["id"] == identifier)
    contract = operation["responseContract"]
    assert contract["kind"] == "json"
    assert_documented(response.json(), contract["schema"])


def test_login_password_and_admin_reset_return_user_contract(user_client):
    logged_in = login(user_client)
    changed = user_client.post("/api/v1/auth/password", headers=CSRF, json={
        "currentPassword": ADMIN_PASSWORD, "newPassword": "contract-changed-password",
    })
    # 首登强制改密完成后才可以读取目录；保存的登录响应仍按真实结果检查。
    assert_response(user_client, "POST /api/v1/auth/login", logged_in)
    assert_response(user_client, "POST /api/v1/auth/password", changed)
    created = user_client.post("/api/v1/users", headers=CSRF, json={
        "username": "contract-user", "displayName": "合同用户", "password": "contract-password", "scopes": [],
    })
    assert_response(user_client, "POST /api/v1/users", created)
    reset = user_client.post(f"/api/v1/users/{created.json()['id']}/reset-password", headers=CSRF, json={
        "version": created.json()["version"], "password": "contract-reset-password",
    })
    assert_response(user_client, "POST /api/v1/users/{identifier}/reset-password", reset)
    assert reset.json()["mustChangePassword"] is True


def test_actual_task_control_and_manual_command_contract(client):
    created = client.post("/api/v1/tasks", headers={"Idempotency-Key": "identity-task"}, json={
        "name": "命令合同任务", "protocol": "SSH", "ip": "127.0.0.1", "port": 22,
        "username": "root", "password": "contract-password", "resourceId": "fixture-device",
    })
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    operation = client.post(f"/api/v1/tasks/{task_id}/start")
    assert_response(client, "POST /api/v1/tasks/{task_id}/start", operation)
    assert_response(client, "GET /api/v1/operations/{identifier}", client.get(f"/api/v1/operations/{operation.json()['id']}"))
    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.update_one, {"id": task_id}, {"$set": {
        "status": "COLLECTING", "desiredState": "RUNNING", "runId": "run", "sessionId": "session", "nodeId": "test",
    }})
    command = client.post(f"/api/v1/tasks/{task_id}/commands", headers={"Idempotency-Key": "identity-command"}, json={"command": "ifconfig"})
    assert_response(client, "POST /api/v1/tasks/{task_id}/commands", command)
    assert_response(client, "GET /api/v1/commands/{identifier}", client.get(f"/api/v1/commands/{command.json()['id']}"))
    assert_response(client, "GET /api/v1/tasks/{task_id}/command-executions", client.get(f"/api/v1/tasks/{task_id}/command-executions"))
