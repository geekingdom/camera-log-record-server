"""定时命令记录可读内容、配置筛选及独立运行预算的接口回归。"""
# ruff: noqa: F811

from camera_logs.common.database import now
from test_api import client  # noqa: F401
from test_task_control import _create_task, _set_task


def test_command_history_filters_and_preserves_execution_snapshot(client):
    """历史快照不可被当前正文覆盖，同正文的不同配置拥有独立筛选及预算。"""
    task, repo = _create_task(client), client.app.state.repo
    commands = [{"id": identifier, "command": "ls", "totalExecutions": 5, "intervalSeconds": 60}
                for identifier in ("a", "b")]
    _set_task(client, task["id"], scheduledCommands=commands, runId="current")
    client.portal.call(repo.db.budgets.insert_many, [
        {"_id": "current:a", "attempts": 2}, {"_id": "current:b", "attempts": 1}, {"_id": "old:a", "attempts": 5},
    ])
    client.portal.call(repo.db.commands.insert_many, [
        {"id": "snapshot", "taskId": task["id"], "commandId": "a", "command": "ps", "kind": "SCHEDULED", "createdAt": now()},
        {"id": "legacy", "taskId": task["id"], "commandId": "b", "kind": "SCHEDULED", "createdAt": now()},
        {"id": "unknown", "taskId": task["id"], "commandId": "removed", "kind": "SCHEDULED", "createdAt": now()},
        {"id": "manual", "taskId": task["id"], "command": "mount", "kind": "MANUAL", "createdAt": now()},
        {"id": "foreign", "taskId": "another", "commandId": "a", "command": "secret", "kind": "SCHEDULED", "createdAt": now()},
    ])
    url = f"/api/v1/tasks/{task['id']}/command-executions"
    result = client.get(url).json()
    records = {item["id"]: item for item in result["items"]}
    assert result["total"] == 4 and result["runId"] == "current"
    assert records["snapshot"]["command"] == "ps" and records["snapshot"]["commandSource"] == "SNAPSHOT"
    assert records["legacy"]["command"] == "ls" and records["legacy"]["commandSource"] == "CURRENT_CONFIGURATION"
    assert records["unknown"].get("command") is None and records["unknown"]["commandSource"] == "UNAVAILABLE"
    assert [(item["id"], item["attempts"]) for item in result["scheduledCommands"]] == [("a", 2), ("b", 1)]
    filtered = client.get(url, params={"commandId": "a", "pageSize": 1}).json()
    assert filtered["total"] == 1 and filtered["items"][0]["id"] == "snapshot"
    assert client.get(url, params={"kind": "MANUAL"}).json()["total"] == 1
    assert client.get(url, params={"kind": "invalid"}).status_code == 422
    assert client.get("/api/v1/tasks/missing/command-executions").status_code == 404


async def test_reservation_stores_immutable_command_content(tmp_path, mock_reservation_transaction):
    """预留记录保存实际配置快照，之后编辑任务不改变历史执行内容。"""
    from test_scheduled_ownership import DETAIL, scheduled_runtime

    runtime = await scheduled_runtime(tmp_path)
    configured = [{"id": "periodic", "command": "ps", "totalExecutions": 2, "intervalSeconds": 17}]
    await runtime.repo.db.tasks.update_one({"id": "task"}, {"$set": {"scheduledCommands": configured}})
    assert await runtime.reserve("periodic", DETAIL)
    await runtime.repo.db.tasks.update_one({"id": "task"}, {"$set": {"scheduledCommands": []}})
    execution = await runtime.repo.db.commands.find_one({"kind": "SCHEDULED"})
    assert execution["command"] == "ps"
    assert execution["intervalSeconds"] == 17 and execution["totalExecutions"] == 2
