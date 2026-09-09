"""验证命令模板的所有者、共享可见性、任务快照和同名索引合同。"""
# ruff: noqa: F811

from datetime import UTC, datetime

from test_api import client  # noqa: F401


def user_token(client, username, scopes):
    """以管理员创建测试用户和服务令牌，令牌请求仍走正式身份解析路径。"""
    user = client.post("/api/v1/users", json={
        "username": username,
        "displayName": username + "显示名",
        "password": username + "-password-123",
        "scopes": scopes,
    }).json()
    token = client.post("/api/v1/service-tokens", json={
        "name": username + "令牌", "userId": user["id"],
    }).json()
    return user, {"Authorization": "Bearer " + token["token"]}


def seed_resource(client):
    """写入任务绑定所需的已认证资源，不连接真实设备。"""
    client.portal.call(client.app.state.repo.db.resources.insert_one, {
        "id": "template-sharing-device", "name": "模板共享设备",
        "kind": "HIKVISION_NETWORK", "ip": "192.0.2.88", "version": 1,
        "deletedAt": None, "model": "DS-2CD", "subSerialNumber": "template-sharing",
        "authenticatedAt": datetime.now(UTC),
    })


def test_template_sharing_owner_uniqueness_and_task_snapshot(client):
    """共享模板只在绑定时授权；任务保存后保留独立快照并不受撤销共享影响。"""
    alice, alice_headers = user_token(client, "template-alice", ["templates:write"])
    bob, bob_headers = user_token(client, "template-bob", ["templates:write", "tasks:create", "tasks:write"])
    shared = client.post("/api/v1/command-templates", headers=alice_headers | {"Idempotency-Key": "alice-template"}, json={
        "name": "共享巡检", "initialCommands": [{"command": "show version"}],
        "scheduledCommands": [{"command": "uptime", "totalExecutions": 2, "intervalSeconds": 5}],
        "sharedWith": [bob["id"]],
    })
    assert shared.status_code == 201, shared.text
    template = shared.json()
    assert template["createdBy"] == alice["id"] and template["createdByName"] == alice["displayName"]
    same_name = client.post("/api/v1/command-templates", headers=bob_headers | {"Idempotency-Key": "bob-same-name"}, json={"name": "共享巡检"})
    assert same_name.status_code == 201, same_name.text
    assert any(item["id"] == template["id"] for item in client.get("/api/v1/command-templates", headers=bob_headers).json()["items"])
    assert client.patch(f"/api/v1/command-templates/{template['id']}", headers=bob_headers, json={"version": 1, "name": "越权修改"}).status_code == 403
    assert client.delete(f"/api/v1/command-templates/{template['id']}?version=1", headers=bob_headers).status_code == 403

    seed_resource(client)
    task = client.post("/api/v1/tasks", headers=bob_headers | {"Idempotency-Key": "shared-template-task"}, json={
        "name": "共享模板任务", "protocol": "SSH", "ip": "192.0.2.88", "port": 22,
        "username": "root", "password": "device-password", "resourceId": "template-sharing-device",
        "sourceTemplateId": template["id"], "sourceTemplateVersion": 0,
        "initialCommands": [{"command": "show version --custom"}],
        "scheduledCommands": [{"command": "uptime --custom", "totalExecutions": 3, "intervalSeconds": 7}],
    })
    assert task.status_code == 201, task.text
    task = task.json()
    assert task["initialCommands"][0]["command"] == "show version --custom"
    assert task["scheduledCommands"][0]["command"] == "uptime --custom"
    assert task["sourceTemplateVersion"] == 0

    revoke = client.patch(f"/api/v1/command-templates/{template['id']}", headers=alice_headers, json={
        "version": template["version"], "name": template["name"], "sharedWith": [], "sharedWithAll": False,
    })
    assert revoke.status_code == 200, revoke.text
    assert client.get(f"/api/v1/command-templates/{template['id']}", headers=bob_headers).status_code == 404
    assert client.delete(f"/api/v1/command-templates/{template['id']}?version={revoke.json()['version']}", headers=alice_headers).status_code == 204
    changed = client.patch(f"/api/v1/tasks/{task['id']}", headers=bob_headers, json={
        "version": task["version"], "description": "模板撤销后继续编辑任务快照",
        "sourceTemplateId": template["id"], "sourceTemplateVersion": task["sourceTemplateVersion"],
        "initialCommands": task["initialCommands"], "scheduledCommands": task["scheduledCommands"],
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["initialCommands"] == task["initialCommands"]


def test_template_soft_delete_is_hidden_by_default_and_owner_can_filter_history(client):
    """删除模板保留任务快照；管理员和创建者可按创建者读取软删除历史。"""
    owner, owner_headers = user_token(client, "template-history-owner", ["templates:write"])
    _other, other_headers = user_token(client, "template-history-other", ["templates:write"])
    first = client.post("/api/v1/command-templates", headers=owner_headers | {"Idempotency-Key": "history-first"}, json={"name": "历史模板"})
    second = client.post("/api/v1/command-templates", headers=other_headers | {"Idempotency-Key": "history-second"}, json={"name": "他人模板"})
    assert first.status_code == second.status_code == 201
    first, second = first.json(), second.json()

    assert client.delete(f"/api/v1/command-templates/{first['id']}?version={first['version']}", headers=owner_headers).status_code == 204
    stored = client.portal.call(client.app.state.repo.db.templates.find_one, {"id": first["id"]})
    assert stored["deletedAt"] is not None and stored["version"] == first["version"] + 1
    assert client.patch(f"/api/v1/command-templates/{first['id']}", headers=owner_headers,
                        json={"version": stored["version"], "name": "不应修改历史模板"}).status_code == 404
    assert client.delete(f"/api/v1/command-templates/{first['id']}?version={stored['version']}",
                         headers=owner_headers).status_code == 404
    assert client.portal.call(client.app.state.repo.db.audit.count_documents,
                             {"action": "delete_template", "targetId": first["id"]}) == 1
    assert all(item["id"] != first["id"] for item in client.get("/api/v1/command-templates", headers=owner_headers).json()["items"])
    history = client.get(f"/api/v1/command-templates?createdBy={owner['id']}&includeDeleted=true", headers=owner_headers)
    assert history.status_code == 200
    assert [item["id"] for item in history.json()["items"]] == [first["id"]]
    assert client.get(f"/api/v1/command-templates?createdBy={owner['id']}&includeDeleted=true", headers=other_headers).json()["items"] == []
    admin_history = client.get(f"/api/v1/command-templates?createdBy={owner['id']}&includeDeleted=true")
    assert [item["id"] for item in admin_history.json()["items"]] == [first["id"]]
    assert all(item["id"] != first["id"] for item in client.get("/api/v1/command-templates", headers=other_headers).json()["items"])


def test_template_public_sharing_requires_admin_and_targets_enabled_users(client):
    """公开共享只能由管理员设置，定向共享拒绝不存在、禁用和已删除用户。"""
    _owner, owner_headers = user_token(client, "template-owner", ["templates:write"])
    disabled, _ = user_token(client, "template-disabled", ["templates:write"])
    repo = client.app.state.repo
    client.portal.call(repo.db.users.update_one, {"id": disabled["id"]}, {"$set": {"enabled": False}})
    assert client.post("/api/v1/command-templates", headers=owner_headers | {"Idempotency-Key": "forbidden-public"}, json={
        "name": "普通用户公开", "sharedWithAll": True,
    }).status_code == 403
    assert client.post("/api/v1/command-templates", headers=owner_headers | {"Idempotency-Key": "invalid-share"}, json={
        "name": "无效共享", "sharedWith": ["missing-user", disabled["id"]],
    }).status_code == 422
    public = client.post("/api/v1/command-templates", headers={"Idempotency-Key": "admin-public"}, json={
        "name": "管理员公开", "sharedWithAll": True,
    })
    assert public.status_code == 201, public.text
    assert any(item["id"] == public.json()["id"] for item in client.get("/api/v1/command-templates", headers=owner_headers).json()["items"])


def test_legacy_template_reads_include_unshared_defaults(client):
    """历史模板未记录共享字段时，公开读模型仍保持当前客户端合同。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.templates.insert_one, {
        "id": "legacy-template", "name": "历史模板", "version": 1,
        "initialCommands": [], "scheduledCommands": [],
    })

    listed = client.get("/api/v1/command-templates")
    assert listed.status_code == 200, listed.text
    legacy = next(item for item in listed.json()["items"] if item["id"] == "legacy-template")
    assert legacy["sharedWith"] == []
    assert legacy["sharedWithAll"] is False
    detail = client.get("/api/v1/command-templates/legacy-template")
    assert detail.status_code == 200, detail.text
    assert detail.json()["sharedWith"] == []
    assert detail.json()["sharedWithAll"] is False
