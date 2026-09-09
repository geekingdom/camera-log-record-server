"""验证管理员请求事件查询只公开脱敏后的排障字段。"""

from datetime import UTC, datetime

pytest_plugins = ("test_api",)


def test_request_event_filters_and_presentation(client):
    """请求事件按 requestId 与失败级别过滤，并补齐任务名称而不公开敏感字段。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.insert_one, {"id": "event-task", "name": "排障任务", "ip": "192.0.2.8"})
    client.portal.call(repo.db.request_events.insert_many, [
        {"createdAt": datetime(2026, 9, 9, tzinfo=UTC), "requestId": "failed-request", "actor": "bootstrap",
         "clientIp": "198.51.100.9", "method": "POST", "route": "/api/v1/tasks/{task_id}/start",
         "httpStatus": 503, "level": "ERROR", "outcome": "FAILED", "taskId": "event-task",
         "reason": "password=must-not-leak", "authorization": "Bearer must-not-leak"},
        {"createdAt": datetime(2026, 9, 9, tzinfo=UTC), "requestId": "other-request", "method": "POST",
         "route": "/api/v1/tasks", "httpStatus": 202, "level": "INFO", "outcome": "SUCCEEDED"},
    ])
    response = client.get("/api/v1/request-events", params={"requestId": "failed-request", "level": "ERROR"})
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["taskName"] == "排障任务" and item["deviceIp"] == "192.0.2.8"
    assert item["outcome"] == "FAILED" and "must-not-leak" not in item["reason"]
    assert "authorization" not in item and "must-not-leak" not in str(item)


def test_derived_filters_use_the_same_values_as_chinese_presentation(client):
    """历史事件未写 level/outcome 时，筛选仍必须匹配页面上展示的推导值。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.resources.insert_one, {
        "id": "resource-event", "name": "受管设备", "ip": "192.0.2.9",
        "passwordEncrypted": "must-not-leak",
    })
    client.portal.call(repo.db.audit.insert_one, {
        "actor": "builtin-admin", "action": "create_resource", "targetId": "resource-event",
        "createdAt": datetime(2026, 9, 9, tzinfo=UTC),
    })
    client.portal.call(repo.db.request_events.insert_one, {
        "createdAt": datetime(2026, 9, 9, tzinfo=UTC), "method": "POST",
        "route": "/api/v1/tasks", "httpStatus": 202, "responseComplete": True,
    })
    client.portal.call(repo.db.request_events.insert_one, {
        "createdAt": datetime(2026, 9, 9, tzinfo=UTC), "requestId": "unfinished-response",
        "method": "POST", "route": "/api/v1/tasks", "httpStatus": 200, "responseComplete": False,
    })

    audit = client.get("/api/v1/audit-events", params={"level": "INFO", "outcome": "SUCCEEDED"})
    request = client.get("/api/v1/request-events", params={"level": "INFO", "outcome": "PENDING"})

    assert audit.status_code == request.status_code == 200
    audit_item = next(item for item in audit.json()["items"] if item["targetId"] == "resource-event")
    assert audit_item["summary"] == "创建设备资源"
    assert audit_item["targetName"] == "受管设备"
    assert "must-not-leak" not in str(audit_item)
    assert request.json()["total"] == 1
    assert request.json()["items"][0]["summary"].startswith("HTTP 请求：POST")
    unknown = client.get("/api/v1/request-events", params={"requestId": "unfinished-response", "outcome": "UNKNOWN"})
    assert unknown.json()["total"] == 1 and unknown.json()["items"][0]["level"] == "WARNING"


def test_derived_filter_paginates_in_database_before_name_enrichment(client, monkeypatch):
    """千条历史事件按推导结果分页时，只允许当前页的 20 个关联 ID 进入名称查询。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.insert_many, [
        {"id": f"bulk-task-{index}", "name": f"批量任务 {index}", "ip": f"192.0.2.{index % 200}"}
        for index in range(1000)
    ])
    client.portal.call(repo.db.events.insert_many, [
        {"taskId": f"bulk-task-{index}", "type": "CONNECTION_GAP",
         "createdAt": datetime(2026, 9, 9, tzinfo=UTC), "sequence": index}
        for index in range(1000)
    ])
    calls = []
    collection_type = type(repo.db.tasks)
    original_find = collection_type.find

    def capture_find(self, query=None, *args, **kwargs):
        if self.name == "tasks" and query and "id" in query:
            calls.append(query["id"]["$in"])
        return original_find(self, query, *args, **kwargs)

    monkeypatch.setattr(collection_type, "find", capture_find)
    response = client.get("/api/v1/runtime-events", params={"outcome": "UNKNOWN", "page": 2, "pageSize": 20})

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1000
    assert len(response.json()["items"]) == 20
    assert calls and max(len(ids) for ids in calls) == 20


def test_derived_level_and_outcome_precedence_and_validation(client):
    """派生级别读取前一阶段结果，显式字段优先，空调试错误不能被误判为失败。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 9, 9, tzinfo=UTC)
    client.portal.call(repo.db.events.insert_many, [
        {"id": "gap", "type": "CONNECTION_GAP", "createdAt": stamp},
        {"id": "debug-empty", "type": "DEBUG_MODE", "debugError": "", "createdAt": stamp},
        {"id": "debug-failed", "type": "DEBUG_MODE", "debugError": "连接失败", "createdAt": stamp},
        {"id": "explicit", "type": "CONNECTION_GAP", "outcome": "FAILED", "level": "WARNING", "createdAt": stamp},
    ])

    warning = client.get("/api/v1/runtime-events", params={"level": "WARNING", "outcome": "UNKNOWN"})
    failed = client.get("/api/v1/runtime-events", params={"level": "ERROR", "outcome": "FAILED"})
    explicit = client.get("/api/v1/runtime-events", params={"level": "WARNING", "outcome": "FAILED"})

    assert warning.status_code == failed.status_code == explicit.status_code == 200
    assert {item["id"] for item in warning.json()["items"]} == {"gap"}
    assert {item["id"] for item in failed.json()["items"]} == {"debug-failed"}
    assert {item["id"] for item in explicit.json()["items"]} == {"explicit"}
    all_items = client.get("/api/v1/runtime-events").json()["items"]
    assert next(item for item in all_items if item["id"] == "debug-empty")["outcome"] == "SUCCEEDED"
    assert client.get("/api/v1/runtime-events?level=BROKEN").status_code == 422
    assert client.get("/api/v1/request-events?outcome=BROKEN").status_code == 422
