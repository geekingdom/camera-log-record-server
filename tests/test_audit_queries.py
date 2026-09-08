"""管理员只能按受限条件查询审计记录和运行事件。"""

import hashlib
from datetime import UTC, datetime, timedelta

from camera_logs.common.database import now

pytest_plugins = ("test_api",)


def test_audit_events_filter_action_actor_task_and_utc_range(client):
    """审计查询组合筛选后只返回匹配记录，且分页总数使用同一条件。"""
    repo = client.app.state.repo
    inside = datetime(2026, 9, 8, 8, tzinfo=UTC)
    client.portal.call(repo.db.audit.insert_many, [
        {"actor": "admin-a", "action": "control:RUNNING", "targetId": "task-a", "createdAt": inside},
        {"actor": "admin-b", "action": "control:RUNNING", "targetId": "task-a", "createdAt": inside},
        {"actor": "admin-a", "action": "edit_task", "targetId": "task-a", "createdAt": inside},
        {"actor": "admin-a", "action": "control:RUNNING", "targetId": "task-b", "createdAt": inside},
        {"actor": "admin-a", "action": "control:RUNNING", "targetId": "task-a", "createdAt": inside - timedelta(days=2)},
    ])
    response = client.get("/api/v1/audit-events", params={
        "action": "control:RUNNING", "actor": "admin-a", "taskId": "task-a",
        "start": "2026-09-08T00:00:00+00:00", "end": "2026-09-09T00:00:00+00:00",
    })
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["targetId"] == "task-a"


def test_runtime_events_filter_task_node_type_and_utc_range(client):
    """运行事件按任务、节点、类别和 UTC 区间独立筛选，不读取日志正文。"""
    repo = client.app.state.repo
    inside = datetime(2026, 9, 8, 8, tzinfo=UTC)
    client.portal.call(repo.db.events.insert_many, [
        {"taskId": "task-a", "nodeId": "node-a", "type": "CONNECTION_GAP", "createdAt": inside},
        {"taskId": "task-b", "nodeId": "node-a", "type": "CONNECTION_GAP", "createdAt": inside},
        {"taskId": "task-a", "nodeId": "node-b", "type": "CONNECTION_GAP", "createdAt": inside},
        {"taskId": "task-a", "nodeId": "node-a", "type": "USER_PAUSED", "createdAt": inside},
        {"taskId": "task-a", "nodeId": "node-a", "type": "CONNECTION_GAP", "createdAt": inside + timedelta(days=2)},
    ])
    response = client.get("/api/v1/runtime-events", params={
        "taskId": "task-a", "nodeId": "node-a", "type": "CONNECTION_GAP",
        "start": "2026-09-08T00:00:00+00:00", "end": "2026-09-09T00:00:00+00:00",
    })
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["type"] == "CONNECTION_GAP"


def test_runtime_events_filter_and_sort_legacy_detected_time(client):
    """连接缺口的历史 detectedAt 字段与新事件同样可筛选并返回统一展示时间。"""
    repo = client.app.state.repo
    detected = datetime(2026, 9, 8, 8, tzinfo=UTC)
    client.portal.call(repo.db.events.insert_one, {
        "taskId": "task-gap", "nodeId": "node-gap", "type": "CONNECTION_GAP", "detectedAt": detected,
    })
    response = client.get("/api/v1/runtime-events", params={
        "taskId": "task-gap", "start": "2026-09-08T00:00:00+00:00", "end": "2026-09-09T00:00:00+00:00",
    })
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["detectedAt"].startswith("2026-09-08T08:00:00")
    assert item["createdAt"].startswith("2026-09-08T08:00:00")


def test_admin_event_queries_reject_naive_or_excessive_time_ranges_and_non_admin(client):
    """筛选时间必须显式 UTC 偏移且上限受控，非管理员得到明确 403。"""
    assert client.get("/api/v1/audit-events", params={
        "start": "2026-09-08T00:00:00", "end": "2026-09-08T01:00:00+00:00",
    }).status_code == 422
    assert client.get("/api/v1/runtime-events", params={
        "start": "2026-08-01T00:00:00+00:00", "end": "2026-09-08T00:00:00+00:00",
    }).status_code == 422

    repo = client.app.state.repo
    token = "read-only-token"
    client.portal.call(repo.db.tokens.insert_one, {
        "id": "read-only", "tokenHash": hashlib.sha256(token.encode()).hexdigest(),
        "scopes": ["tasks:read"], "taskIds": None, "revoked": False,
        "expiresAt": now() + timedelta(days=1), "createdAt": now(),
    })
    response = client.get("/api/v1/audit-events", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
