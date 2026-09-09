"""正式手动命令接口的审计、会话变化重放和幂等键边界。"""
# ruff: noqa: F811
from test_api import client  # noqa: F401


def test_retry_after_stop_returns_original_command_and_single_audit(client):
    db = client.app.state.repo.db
    client.portal.call(db.tasks.insert_one, {"id": "task", "status": "COLLECTING", "desiredState": "RUNNING",
                                           "runId": "run", "sessionId": "session"})
    headers = {"Idempotency-Key": "manual-once"}
    first = client.post("/api/v1/tasks/task/commands", json={"command": "ls"}, headers=headers)
    assert first.status_code == 202
    identifier = first.json()["id"]
    client.portal.call(db.tasks.update_one, {"id": "task"}, {"$set": {"status": "STOPPED", "desiredState": "STOPPED"}})
    replay = client.post("/api/v1/tasks/task/commands", json={"command": "ls"}, headers=headers)
    assert replay.status_code == 202 and replay.json()["id"] == identifier
    assert client.portal.call(db.commands.count_documents, {}) == 1
    assert client.portal.call(db.audit.count_documents, {"action": "command:task", "targetId": identifier}) == 1
    mapping = client.portal.call(db.idempotency.find_one, {"key": "manual-once"})
    assert mapping["state"] == "SUCCEEDED"
    assert client.post("/api/v1/tasks/task/commands", json={"command": "pwd"}, headers=headers).status_code == 409
