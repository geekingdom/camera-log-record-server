"""正式日志作业接口的原子创建、校验与幂等重放回归。"""
# ruff: noqa: F811
import pytest
from camera_logs.common import audited_mutations
from test_api import client  # noqa: F401

HOUR = "2026-09-09T10:00:00+00:00"


def seed(client):
    """仅在内存数据库构造目录，无真实日志正文或设备连接。"""
    db = client.app.state.repo.db
    client.portal.call(db.tasks.insert_one, {"id": "task", "name": "模拟任务", "ip": "192.0.2.1"})
    client.portal.call(db.files.insert_one, {"id": "file", "taskId": "task", "nodeId": "node",
                                           "hour": HOUR, "status": "READY", "bytes": 20})
    return db


@pytest.mark.parametrize(("path", "body", "kind"), [
    ("/downloads", {"taskId": "task", "hourIds": [HOUR]}, "DOWNLOAD"),
    ("/log-searches", {"taskId": "task", "keyword": "needle", "start": HOUR,
                       "end": "2026-09-09T11:00:00+00:00"}, "SEARCH"),
])
def test_job_creation_publishes_success_mapping_and_replays_snapshot(client, path, body, kind):
    db = seed(client)
    response = client.post("/api/v1" + path, json=body, headers={"Idempotency-Key": "once"})
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert client.portal.call(db.idempotency.find_one, {"key": "once"})["state"] == "SUCCEEDED"
    client.portal.call(db.files.update_one, {"id": "file"}, {"$set": {"bytes": 999}})
    again = client.post("/api/v1" + path, json=body, headers={"Idempotency-Key": "once"})
    assert again.status_code == 202 and again.json()["files"][0]["bytes"] == 20
    assert client.portal.call(db.jobs.count_documents, {}) == 1
    assert client.portal.call(db.audit.count_documents, {"action": kind, "targetId": identifier}) == 1


def test_missing_hour_does_not_extend_file_protection(client):
    db = seed(client)
    response = client.post("/api/v1/downloads", json={"taskId": "task", "hourIds": [HOUR, "missing"]},
                           headers={"Idempotency-Key": "bad-range"})
    assert response.status_code == 409
    assert "retainUntil" not in client.portal.call(db.files.find_one, {"id": "file"})
    assert client.portal.call(db.jobs.count_documents, {}) == 0


def test_job_cancel_is_transactional_and_retries_do_not_duplicate_audit(client, monkeypatch):
    db = seed(client)
    client.portal.call(db.jobs.insert_one, {"id": "job", "taskId": "task", "actor": "bootstrap",
                                          "kind": "DOWNLOAD", "status": "RUNNING"})
    original, calls = audited_mutations.mutation_transaction, []

    async def tracked(repo, callback):
        calls.append(True)
        return await original(repo, callback)

    monkeypatch.setattr(audited_mutations, "mutation_transaction", tracked)
    for _ in range(2):
        assert client.delete("/api/v1/downloads/job").status_code == 204
    assert calls
    assert client.portal.call(db.audit.count_documents, {"action": "cancel_download", "targetId": "job"}) == 1
