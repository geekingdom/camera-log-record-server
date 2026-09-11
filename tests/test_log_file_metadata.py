"""日志文件公开元数据接口不得泄露 Worker 文件系统路径。"""

from camera_logs.common.security import actor
from test_api import client  # noqa: F401


def test_log_file_metadata_authorizes_task_and_returns_only_safe_fields(client):  # noqa: F811
    """文件定位接口按任务日志权限读取，宿主机路径和索引不得进入响应。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.insert_one, {"id": "metadata-task", "name": "任务", "ip": "192.0.2.9"})
    client.portal.call(repo.db.files.insert_one, {
        "id": "metadata-file", "taskId": "metadata-task", "runId": "run", "sessionId": "session",
        "nodeId": "node", "status": "READY", "bytes": 42, "hour": "2026-09-11T01:00:00+00:00",
        "archiveName": "hour.tar.gz", "rawFileName": "part-000001.log", "segmentNumber": 1,
        "firstSequence": 7, "lastSequence": 9, "path": "/private/log.tar.gz",
        "rawPath": "/private/part.log", "indexPath": "/private/part.index.jsonl",
    })

    response = client.get("/api/v1/log-files/metadata-file")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": "metadata-file", "taskId": "metadata-task", "runId": "run", "sessionId": "session",
        "nodeId": "node", "status": "READY", "bytes": 42, "hour": "2026-09-11T01:00:00+00:00",
        "archiveName": "hour.tar.gz", "rawFileName": "part-000001.log", "segmentNumber": 1,
        "firstSequence": 7, "lastSequence": 9,
    }


def test_log_file_metadata_requires_login_and_logs_read_scope(client):  # noqa: F811
    """文件元数据与内容读取使用相同的任务日志读取权限。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.insert_one, {"id": "metadata-auth-task", "name": "任务", "ip": "192.0.2.10"})
    client.portal.call(repo.db.files.insert_one, {
        "id": "metadata-auth-file", "taskId": "metadata-auth-task", "nodeId": "node", "status": "READY",
    })
    assert client.get("/api/v1/log-files/metadata-auth-file", headers={"Authorization": ""}).status_code == 401
    client.app.dependency_overrides[actor] = lambda: {"id": "limited", "scopes": ["tasks:read"], "isAdmin": False}
    try:
        assert client.get("/api/v1/log-files/metadata-auth-file").status_code == 403
    finally:
        client.app.dependency_overrides.clear()
