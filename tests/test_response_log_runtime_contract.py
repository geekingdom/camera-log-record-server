"""验证日志运行态内部字段不会破坏严格公开响应合同。"""

from datetime import UTC, datetime, timedelta

import pytest
from test_api import client as _api_client


@pytest.fixture(name="client")
def client_fixture(tmp_path):
    """复用正式 API 夹具，使路由经过实际响应模型校验。"""
    yield from _api_client.__wrapped__(tmp_path)


def test_log_hour_hides_deployment_recovery_marker(client):
    """部署预备期间的路径和令牌只能供 Worker 收尾，不能进入 files[]。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 10, 21, tzinfo=UTC)
    task = {
        "id": "runtime-contract-log", "name": "运行态合同日志", "ip": "127.0.0.1",
        "createdBy": "bootstrap", "status": "STOPPED", "desiredState": "STOPPED",
    }
    marker = {
        "token": "deployment-recovery-token", "nodeId": "test", "taskId": task["id"],
        "runId": "run", "generation": 3, "sessionId": "session",
        "path": "/private/var/lib/camera-logs/raw.log", "watermark": 12,
    }
    client.portal.call(repo.db.tasks.insert_one, task)
    client.portal.call(repo.db.files.insert_one, {
        "id": "runtime-contract-file", "taskId": task["id"], "runId": "run",
        "sessionId": "session", "nodeId": "test", "status": "OPEN", "bytes": 12,
        "hour": stamp.isoformat(), "path": "/private/var/lib/camera-logs/raw.log",
        "deploymentRecovery": marker,
    })

    response = client.get(f"/api/v1/tasks/{task['id']}/log-hours")

    assert response.status_code == 200, response.text
    file = response.json()["items"][0]["files"][0]
    assert "deploymentRecovery" not in file
    assert marker["token"] not in response.text
    assert marker["path"] not in response.text

    download = client.post("/api/v1/downloads", headers={"Idempotency-Key": "runtime-contract-download"}, json={
        "taskId": task["id"], "hourIds": [stamp.isoformat()], "allowPartial": False,
    })
    assert download.status_code == 202, download.text
    assert "deploymentRecovery" not in download.json()["files"][0]
    assert marker["token"] not in download.text


def test_job_hides_output_recovery_reason(client):
    """内核锁恢复原因只用于节点清理，读取作业不得触发额外字段校验。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 10, 21, tzinfo=UTC)
    task = {
        "id": "runtime-contract-download", "name": "运行态合同下载", "ip": "127.0.0.1",
        "createdBy": "bootstrap", "status": "STOPPED", "desiredState": "STOPPED",
    }
    client.portal.call(repo.db.tasks.insert_one, task)
    client.portal.call(repo.db.jobs.insert_one, {
        "id": "runtime-contract-job", "kind": "DOWNLOAD", "taskId": task["id"],
        "nodeId": "test", "status": "SUCCEEDED", "progress": 100, "actor": "bootstrap",
        "taskName": task["name"], "taskIp": task["ip"], "createdAt": stamp,
        "expiresAt": stamp + timedelta(hours=1), "executionExpiresAt": stamp + timedelta(hours=1),
        "files": [{
            "id": "legacy-job-file", "taskId": task["id"], "status": "OPEN", "bytes": 1,
            "hour": stamp, "deploymentRecovery": {"token": "legacy-deployment-token", "path": "/private/raw.log"},
        }],
        "hourIds": [], "allowPartial": False,
        "outputWriterClosureReason": "KERNEL_LOCK_RECOVERY",
    })

    response = client.get("/api/v1/downloads/runtime-contract-job")

    assert response.status_code == 200, response.text
    assert "outputWriterClosureReason" not in response.json()
    assert "deploymentRecovery" not in response.json()["files"][0]
    assert "legacy-deployment-token" not in response.text
