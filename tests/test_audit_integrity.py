"""审计事实与人工隔离依据的回归测试。"""
import json
from datetime import timedelta

import pytest
from camera_logs.common.database import now
from test_api import client  # noqa: F401


@pytest.mark.parametrize(("identifier", "kind", "path"), [
    ("download-job", "DOWNLOAD", "/api/v1/downloads/download-job"),
    ("search-job", "SEARCH", "/api/v1/log-searches/search-job"),
])
def test_cancelling_active_job_writes_typed_audit_event(client, identifier, kind, path):  # noqa: F811
    """用户取消仍在执行的下载或检索后，审计必须反映实际状态转换。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.jobs.insert_one, {
        "id": identifier, "taskId": "task-a", "kind": kind, "actor": "bootstrap", "status": "RUNNING",
    })

    response = client.delete(path)

    assert response.status_code == 204
    job = client.portal.call(repo.db.jobs.find_one, {"id": identifier})
    audit = client.portal.call(repo.db.audit.find_one, {"targetId": identifier})
    assert job["status"] == "CANCELLED"
    assert audit["actor"] == "bootstrap"
    assert audit["action"] == f"cancel_{kind.lower()}"


def test_missing_service_token_does_not_create_revoke_audit(client):  # noqa: F811
    """不存在的令牌必须返回 404，不能留下已撤销的虚假审计事件。"""
    repo = client.app.state.repo

    response = client.delete("/api/v1/service-tokens/missing-token")

    assert response.status_code == 404
    assert client.portal.call(repo.db.audit.count_documents, {
        "action": "revoke_token", "targetId": "missing-token",
    }) == 0


def test_isolation_evidence_is_recursively_redacted_before_runtime_event_output(client, monkeypatch):  # noqa: F811
    """JSON 隔离依据中的嵌套凭据不能进入存储或管理员运行事件响应。"""
    repo = client.app.state.repo
    async def mock_transaction(_repo, callback):
        """该用例只验证脱敏，真实事务回滚由副本集验收脚本验证。"""
        return await callback(None)
    monkeypatch.setattr("camera_logs.administration.isolation.isolation_transaction", mock_transaction)
    client.portal.call(repo.db.nodes.insert_one, {
        "id": "stale-node", "heartbeat": now() - timedelta(seconds=31), "accepting": True,
    })
    evidence = json.dumps({
        "ticket": "OPS-123", "credentials": {"password": "camera-secret"},
        "authorization": "Bearer evidence-token", "nested": [{"token": "nested-token"}],
    })

    response = client.post("/api/v1/nodes/stale-node/confirm-isolation", json={
        "confirmation": "CONFIRM_NODE_ISOLATED", "evidence": evidence,
    })

    assert response.status_code == 200, response.text
    event = client.portal.call(repo.db.events.find_one, {"nodeId": "stale-node"})
    runtime = client.get("/api/v1/runtime-events?nodeId=stale-node")
    assert runtime.status_code == 200, runtime.text
    stored = event["evidence"]
    returned = runtime.json()["items"][0]["evidence"]
    for secret in ("camera-secret", "evidence-token", "nested-token"):
        assert secret not in stored
        assert secret not in returned
    assert json.loads(stored)["credentials"]["password"] == "[REDACTED]"
    assert json.loads(returned)["nested"][0]["token"] == "[REDACTED]"
