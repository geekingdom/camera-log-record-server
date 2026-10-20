"""验证管理员全局常用命令的维护边界与所有用户的快捷读取合同。"""

# ruff: noqa: F811

from test_api import client  # noqa: F401
from test_template_sharing import user_token


def test_admin_manages_common_commands_and_all_users_read_enabled_entries(client):
    """管理员创建、编辑、禁用和删除；普通用户仅收到仍可使用的命令。"""
    _operator, operator_headers = user_token(client, "common-command-reader", [])

    forbidden = client.post("/api/v1/admin/common-commands", headers=operator_headers | {"Idempotency-Key": "forbidden"}, json={
        "command": "show version", "description": "查看版本",
    })
    assert forbidden.status_code == 403

    created = client.post("/api/v1/admin/common-commands", headers={"Idempotency-Key": "create-common-command"}, json={
        "command": " show version ", "description": " 查看版本 ",
    })
    assert created.status_code == 201, created.text
    command = created.json()
    assert command["command"] == "show version"
    assert command["description"] == "查看版本"
    assert command["enabled"] is True
    assert command["version"] == 1
    replay = client.post("/api/v1/admin/common-commands", headers={"Idempotency-Key": "create-common-command"}, json={
        "command": " show version ", "description": " 查看版本 ",
    })
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == command["id"]

    visible = client.get("/api/v1/common-commands", headers=operator_headers)
    assert visible.status_code == 200, visible.text
    assert visible.json()["items"][0] | {"createdAt": command["createdAt"], "updatedAt": command["updatedAt"]} == command

    changed = client.patch(f"/api/v1/admin/common-commands/{command['id']}", json={
        "version": command["version"], "description": "查看设备软件版本", "enabled": False,
    })
    assert changed.status_code == 200, changed.text
    changed = changed.json()
    assert changed["enabled"] is False
    assert changed["description"] == "查看设备软件版本"
    assert changed["version"] == 2
    assert client.get("/api/v1/common-commands", headers=operator_headers).json()["items"] == []

    admin_items = client.get("/api/v1/admin/common-commands")
    assert admin_items.status_code == 200, admin_items.text
    assert admin_items.json()["items"] == [changed]
    assert client.delete(f"/api/v1/admin/common-commands/{command['id']}?version={changed['version']}").status_code == 204
    assert client.get("/api/v1/admin/common-commands").json()["items"] == []


def test_common_command_rejects_duplicate_stale_and_invalid_updates(client):
    """命令文本全局唯一，编辑与删除均由版本锁避免管理员互相覆盖。"""
    first = client.post("/api/v1/admin/common-commands", headers={"Idempotency-Key": "common-first"}, json={
        "command": "ifconfig", "description": "网络地址",
    })
    assert first.status_code == 201, first.text
    first = first.json()
    assert client.post("/api/v1/admin/common-commands", headers={"Idempotency-Key": "common-duplicate"}, json={
        "command": "ifconfig", "description": "重复",
    }).status_code == 409
    assert client.patch(f"/api/v1/admin/common-commands/{first['id']}", json={
        "version": first["version"],
    }).status_code == 422
    assert client.patch(f"/api/v1/admin/common-commands/{first['id']}", json={
        "version": first["version"] + 1, "enabled": False,
    }).status_code == 409
    assert client.delete(f"/api/v1/admin/common-commands/{first['id']}?version={first['version'] + 1}").status_code == 409
    assert client.portal.call(client.app.state.repo.db.audit.count_documents, {
        "action": "create_common_command", "targetId": first["id"],
    }) == 1
    assert client.portal.call(client.app.state.repo.db.audit.count_documents, {
        "action": "update_common_command", "targetId": first["id"],
    }) == 0


def test_admin_common_command_filters_pagination_and_atomic_batch(client):
    """管理员列表可筛选，批量命令以全部快照版本原子变更且可幂等重放。"""
    db = client.app.state.repo.db
    commands = [
        {"id": f"common-{number}", "command": f"show {number:02}", "description": "网络检查" if number == 0 else "系统检查",
         "enabled": number % 2 == 0, "version": 1}
        for number in range(25)
    ]
    client.portal.call(db.common_commands.insert_many, commands)
    first = client.get("/api/v1/admin/common-commands")
    assert first.status_code == 200
    assert first.json()["pageSize"] == 20 and len(first.json()["items"]) == 20
    disabled = client.get("/api/v1/admin/common-commands?enabled=false&query=%E7%B3%BB%E7%BB%9F")
    assert disabled.status_code == 200
    assert disabled.json()["total"] == 12 and all(not item["enabled"] for item in disabled.json()["items"])

    selected = [{"id": "common-1", "version": 1}, {"id": "common-3", "version": 1}]
    response = client.post("/api/v1/admin/common-commands/batch", headers={"Idempotency-Key": "batch-enable"}, json={
        "action": "ENABLE", "items": selected,
    })
    assert response.status_code == 200, response.text
    assert response.json() == {"action": "ENABLE", "affected": 2}
    replay = client.post("/api/v1/admin/common-commands/batch", headers={"Idempotency-Key": "batch-enable"}, json={
        "action": "ENABLE", "items": selected,
    })
    assert replay.json() == response.json()
    assert client.portal.call(db.common_commands.find_one, {"id": "common-1"})["version"] == 2
    assert client.portal.call(db.audit.count_documents, {"action": "batch_enable_common_commands"}) == 2

    stale = client.post("/api/v1/admin/common-commands/batch", headers={"Idempotency-Key": "batch-stale"}, json={
        "action": "DISABLE", "items": [{"id": "common-1", "version": 1}, {"id": "common-5", "version": 1}],
    })
    assert stale.status_code == 409
    assert client.portal.call(db.common_commands.find_one, {"id": "common-5"})["version"] == 1
