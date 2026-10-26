"""事件响应合同覆盖展示器当前产生的公开字段。"""

# ruff: noqa: F811
from camera_logs.administration.event_presenter import present_events
from camera_logs.common.api_contracts.response_events import EVENT
from test_api import client  # noqa: F401
from test_response_admin_contract import assert_documented


def _keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*[_keys(item) for item in value.values()]) if value else set()
    if isinstance(value, list):
        return set().union(*[_keys(item) for item in value]) if value else set()
    return set()


def test_event_contract_documents_real_presenter_output(client):
    """真实展示器补齐的来源、压力、迁移和调试字段必须逐项有中文合同。"""
    db = client.app.state.repo.db
    raw = {
        "id": "event",
        "actor": "system",
        "type": "DEBUG_MODE",
        "phase": "FAILED",
        "mode": "PSH",
        "debugDiagnostic": {"stage": "challenge"},
        "taskId": "task",
        "runId": "run",
        "sessionId": "session",
        "nodeId": "node",
        "oldNodeId": "old",
        "newNodeId": "new",
        "diskPercent": 91,
        "previousLevel": "NORMAL",
        "writeLatencyMs": 300,
        "writeLatencyLimitMs": 200,
        "writeLatencySamples": 2,
        "writeLatencyPendingMs": 10,
        "writeLatencyWindowSeconds": 60,
        "previousReceivedAt": "2026-10-20T12:00:00",
        "receivedAt": "2026-10-20T11:00:00",
        "sequence": 2,
    }
    actual = client.portal.call(present_events, db, [raw])[0]
    assert_documented(actual, EVENT)


def test_request_event_actual_middleware_fields_are_documented(client):
    """由真实中间件记录正常与校验失败响应，覆盖目标分类和可空异常字段。"""
    client.get("/api/v1/platform-settings")
    client.get("/api/v1/tasks?page=-1")

    async def events():
        rows = [row async for row in client.app.state.repo.db.request_events.find({})]
        return await present_events(client.app.state.repo.db, rows)

    rows = client.portal.call(events)
    assert len(rows) >= 2
    for row in rows:
        assert_documented(row, EVENT)


def test_user_resource_disposition_audit_metadata_is_documented(client):
    """用户资源处置的真实审计保留原归属和接收者，并逐字段生成中文文档。"""
    repo = client.app.state.repo

    async def produce():
        await repo.audit("bootstrap", "transfer_user_resource", "resource",
                         metadata={"createdBy": "old-user", "targetUserId": "new-user"})
        return await present_events(repo.db, [row async for row in repo.db.audit.find({})])

    rows = client.portal.call(produce)
    assert rows[0]["metadata"] == {"createdBy": "old-user", "targetUserId": "new-user"}
    assert_documented(rows[0], EVENT)


def test_runtime_event_producers_have_documented_fields(client):
    """实际从机、调试和时钟回拨写入器生成事件，再经公开展示器核对嵌套字段。"""
    from types import SimpleNamespace

    from camera_logs.collection.psh_diagnostics import diagnostic, started_at
    from camera_logs.collection.runtime_events import _record_clock_rollback, record_debug_event
    from camera_logs.collection.slave_events import record_slave_bootstrap_event

    repo = client.app.state.repo
    task = {"id": "task", "runId": "run", "generation": 1, "resourceId": "resource", "nodeId": "node"}
    runtime = SimpleNamespace(
        repo=repo,
        task=task,
        collector=SimpleNamespace(session_id="session"),
        retired=True,
        file_id=lambda path: "file",
    )

    async def produce():
        await record_slave_bootstrap_event(
            repo, task, phase="FAILED", outcome="FAILED", level="ERROR", message="测试阶段失败", port=22
        )
        await record_debug_event(
            runtime,
            "FAILED",
            {
                "mode": "PSH",
                "debugDiagnostic": diagnostic(
                    "decrypt",
                    "HTTP_ERROR",
                    started=started_at(),
                    endpoint="https://example.invalid/api",
                    status=503,
                    payload={"code": 1, "message": "测试错误"},
                    error=ValueError("测试错误"),
                ),
            },
        )
        await _record_clock_rollback(
            runtime,
            {
                "sessionId": "session",
                "previousReceivedAt": "2026-10-20T12:00:00",
                "receivedAt": "2026-10-20T11:00:00",
                "sequence": 2,
                "path": "file",
            },
        )
        return await present_events(repo.db, [row async for row in repo.db.events.find({})])

    rows = client.portal.call(produce)
    assert len(rows) == 3
    for row in rows:
        assert_documented(row, EVENT)
