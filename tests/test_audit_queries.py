"""管理员只能按受限条件查询审计记录和运行事件。"""

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.administration.api import _runtime_event_page
from camera_logs.common.database import now

pytest_plugins = ("test_api",)


@pytest.mark.usefixtures("awaitable_mongomock_event_aggregate")
@pytest.mark.parametrize("event_type,summary", [
    ("IDLE_TIMEOUT", "采集日志空闲超时"), ("READ_ERROR", "采集连接读取失败"),
])
def test_reconnect_cause_summary_and_warning_filter_agree(client, event_type, summary):
    """重连原因保留会话定位，列表与结果/级别筛选不能把异常误标成成功。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.events.insert_one, {
        "id": "reconnect-cause", "taskId": "cause-task", "runId": "cause-run",
        "sessionId": "cause-session", "nodeId": "cause-node", "type": event_type,
        "createdAt": now(), "message": "连接将由原运行实例重试",
    })
    query = {"type": event_type, "taskId": "cause-task", "outcome": "UNKNOWN", "level": "WARNING"}
    response = client.get("/api/v1/runtime-events", params=query)
    assert response.status_code == 200
    assert response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["summary"] == summary
    assert item["sessionId"] == "cause-session" and item["runId"] == "cause-run"
    assert item["reason"] == "连接将由原运行实例重试"
    succeeded = client.get("/api/v1/runtime-events", params={"type": event_type, "outcome": "SUCCEEDED"})
    assert succeeded.json()["total"] == 0


@pytest.mark.usefixtures("awaitable_mongomock_event_aggregate")
def test_coredump_failure_summary_target_and_filter_agree(client):
    """核心转储失败审计的中文摘要、导出名称和数据库失败筛选一致。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.coredump_exports.insert_one, {
        "id": "core-export", "filename": "device-core.zip", "resultPath": "/private/path",
    })
    client.portal.call(repo.db.audit.insert_one, {
        "actor": "bootstrap", "action": "coredump_export_failed", "targetId": "core-export", "createdAt": now(),
    })
    response = client.get("/api/v1/audit-events", params={"outcome": "FAILED", "level": "ERROR"})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["summary"] == "核心转储导出失败"
    assert item["targetName"] == "device-core.zip"
    assert "/private/path" not in response.text


def test_resource_offline_audit_identifies_device(client):
    """系统停止动作展示资源名称/IP，成功执行停止与设备离线原因分别表达。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.resources.insert_one, {"id": "offline", "name": "离线设备", "ip": "192.0.2.4"})
    client.portal.call(repo.db.audit.insert_one, {
        "actor": "system", "action": "resource_health_stop:OFFLINE", "targetId": "offline", "createdAt": now(),
    })
    item = client.get("/api/v1/audit-events").json()["items"][0]
    assert item["summary"] == "设备离线，系统停止关联采集"
    assert item["targetName"] == "离线设备" and item["deviceIp"] == "192.0.2.4"


@pytest.mark.parametrize("action,summary", [("reveal_token", "查看服务账号口令"), ("rotate_token", "重新生成服务账号口令")])
def test_service_credential_audit_names_target_without_secret(client, action, summary):
    """口令操作可定位对应账号，但目标补充查询不能把摘要或加密口令带入审计页面。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.tokens.insert_one, {
        "id": "credential-audit-target", "name": "集成服务账号", "tokenHash": "hidden-hash",
        "tokenEncrypted": "hidden-ciphertext",
    })
    client.portal.call(repo.db.audit.insert_one, {
        "actor": "bootstrap", "action": action, "targetId": "credential-audit-target", "createdAt": now(),
    })
    response = client.get("/api/v1/audit-events", params={"action": action})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["summary"] == summary and item["targetName"] == "集成服务账号"
    assert "hidden-" not in response.text


async def test_runtime_event_page_awaits_production_aggregate_cursor():
    """生产驱动的 aggregate 是协程，分页必须等待它返回异步游标。"""
    async def cursor():
        yield {"type": "CONNECTION_GAP", "detectedAt": datetime(2026, 9, 8, 8, tzinfo=UTC)}

    aggregate = AsyncMock(return_value=cursor())
    count_documents = AsyncMock(return_value=1)
    db = SimpleNamespace(events=SimpleNamespace(aggregate=aggregate, count_documents=count_documents))

    result = await _runtime_event_page(db, {"taskId": "task-gap"}, 1, 20)

    aggregate.assert_awaited_once()
    count_documents.assert_awaited_once_with({"taskId": "task-gap"})
    assert result["items"] == [{"type": "CONNECTION_GAP", "detectedAt": datetime(2026, 9, 8, 8, tzinfo=UTC),
                                "createdAt": datetime(2026, 9, 8, 8, tzinfo=UTC)}]


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


@pytest.mark.usefixtures("awaitable_mongomock_event_aggregate")
def test_login_failed_audit_derives_failed_error_and_supports_filters(client):
    """认证失败审计必须展示为失败错误，并与 Mongo 派生筛选保持一致。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.audit.insert_one, {
        "actor": "anonymous", "action": "login_failed", "targetId": None, "createdAt": now(),
    })

    response = client.get("/api/v1/audit-events", params={"level": "ERROR", "outcome": "FAILED"})

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["action"] == "login_failed"
    assert item["outcome"] == "FAILED" and item["level"] == "ERROR"


@pytest.mark.usefixtures("awaitable_mongomock_event_aggregate")
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
    item = response.json()["items"][0]
    assert item["type"] == "CONNECTION_GAP"
    assert item["summary"] == "采集连接中断"
    assert item["outcome"] == "UNKNOWN" and item["level"] == "WARNING"


@pytest.mark.usefixtures("awaitable_mongomock_event_aggregate")
def test_coredump_unmount_events_have_chinese_summary_and_derived_filters(client):
    """卸载后任务不再是活跃 owner，运行事件仍须可按中文结果和派生筛选追溯。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 9, 8, 8, tzinfo=UTC)
    client.portal.call(repo.db.events.insert_many, [
        {"id": "unmounted", "taskId": "task-a", "type": "COREDUMP_MOUNT", "status": "UNMOUNTED", "createdAt": stamp},
        {"id": "unmount-skipped", "taskId": "task-a", "type": "COREDUMP_MOUNT", "status": "UNMOUNT_SKIPPED", "createdAt": stamp},
        {"id": "unmount-failed", "taskId": "task-a", "type": "COREDUMP_MOUNT", "status": "UNMOUNT_FAILED", "createdAt": stamp},
    ])

    succeeded = client.get("/api/v1/runtime-events", params={"outcome": "SUCCEEDED", "level": "INFO"})
    unknown = client.get("/api/v1/runtime-events", params={"outcome": "UNKNOWN", "level": "WARNING"})
    failed = client.get("/api/v1/runtime-events", params={"outcome": "FAILED", "level": "ERROR"})

    assert succeeded.status_code == unknown.status_code == failed.status_code == 200
    assert succeeded.json()["total"] == unknown.json()["total"] == failed.json()["total"] == 1
    success_item, unknown_item, failed_item = (
        succeeded.json()["items"][0], unknown.json()["items"][0], failed.json()["items"][0],
    )
    assert success_item["id"] == "unmounted"
    assert success_item["summary"] == "核心转储 NFS 已卸载"
    assert success_item["outcome"] == "SUCCEEDED" and success_item["level"] == "INFO"
    assert unknown_item["id"] == "unmount-skipped"
    assert unknown_item["summary"] == "核心转储 NFS 跳过卸载，结果未确认"
    assert unknown_item["outcome"] == "UNKNOWN" and unknown_item["level"] == "WARNING"
    assert failed_item["id"] == "unmount-failed"
    assert failed_item["summary"] == "核心转储 NFS 卸载失败"
    assert failed_item["outcome"] == "FAILED" and failed_item["level"] == "ERROR"


@pytest.mark.usefixtures("awaitable_mongomock_event_aggregate")
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
    client.portal.call(repo.db.users.insert_one, {
        "id": "read-only-user", "username": "read-only-user", "displayName": "只读用户",
        "isAdmin": False, "scopes": [], "enabled": True, "deletedAt": None,
    })
    client.portal.call(repo.db.tokens.insert_one, {
        "id": "read-only", "userId": "read-only-user", "version": 1,
        "tokenHash": hashlib.sha256(token.encode()).hexdigest(), "revoked": False,
        "expiresAt": now() + timedelta(days=1), "createdAt": now(),
    })
    response = client.get("/api/v1/audit-events", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
