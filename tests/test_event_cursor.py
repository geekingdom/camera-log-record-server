"""验证管理事件游标分页保持旧页码契约，并绑定查询与运行时间模式。"""

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from camera_logs.administration.event_cursor import after_event_cursor_clause

pytest_plugins = ("test_api",)


def _params(**items):
    """剔除空参数，避免测试客户端把未传游标与空游标混为一谈。"""
    return {key: value for key, value in items.items() if value is not None}


def test_object_id_cursor_explicitly_continues_to_string_id_group():
    """真实 Mongo 的比较条件会按 BSON 类型分组，ObjectId 锚点必须显式纳入字符串组。"""
    anchor = ObjectId()
    clause = after_event_cursor_clause(time_field="createdAt", event_time=datetime(2026, 9, 14, tzinfo=UTC),
                                       identifier=anchor)
    same_time = clause["$or"][1]["$or"]
    assert {"_id": {"$type": "string"}} in same_time
    assert {"_id": {"$type": "objectId", "$lt": anchor}} in same_time


@pytest.mark.usefixtures("awaitable_mongomock_event_aggregate")
def test_audit_cursor_uses_raw_mixed_id_tie_breaker_without_count_or_skip(client, monkeypatch):
    """相同时间下混合 ObjectId/字符串主键必须跨页完整返回，游标首屏默认不统计总数。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 9, 14, 8, tzinfo=UTC)
    client.portal.call(repo.db.audit.insert_many, [
        {"_id": ObjectId(), "id": "object-a", "actor": "bootstrap", "action": "cursor", "createdAt": stamp},
        {"_id": "legacy-z", "id": "string-z", "actor": "bootstrap", "action": "cursor", "createdAt": stamp},
        {"_id": "legacy-a", "id": "string-a", "actor": "bootstrap", "action": "cursor", "createdAt": stamp},
        {"_id": ObjectId(), "id": "object-b", "actor": "bootstrap", "action": "cursor", "createdAt": stamp - timedelta(seconds=1)},
    ])

    async def count_forbidden(*_args, **_kwargs):
        raise AssertionError("默认游标请求不得统计总数")

    monkeypatch.setattr(repo.db.audit, "count_documents", count_forbidden)
    first = client.get("/api/v1/audit-events", params=_params(action="cursor", pageSize=2, cursor=""))
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["total"] is None and first_body["pageSize"] == 2 and first_body["hasMore"] is True
    assert first_body["nextCursor"]

    second = client.get("/api/v1/audit-events", params=_params(action="cursor", pageSize=2, cursor=first_body["nextCursor"]))
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["total"] is None and second_body["hasMore"] is False and second_body["nextCursor"] is None
    identifiers = [item["id"] for item in first_body["items"] + second_body["items"]]
    assert len(identifiers) == len(set(identifiers)) == 4
    assert set(identifiers) == {"object-a", "object-b", "string-z", "string-a"}


@pytest.mark.usefixtures("awaitable_mongomock_event_aggregate")
def test_event_cursor_binds_filter_and_only_counts_when_requested(client):
    """游标不得跨集合或完整筛选复用；显式 includeTotal 才执行计数。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 9, 14, 8, tzinfo=UTC)
    client.portal.call(repo.db.request_events.insert_many, [
        {"id": "request-a", "route": "/api/v1/a", "httpStatus": 200, "createdAt": stamp},
        {"id": "request-b", "route": "/api/v1/a", "httpStatus": 200, "createdAt": stamp - timedelta(seconds=1)},
        {"id": "request-other", "route": "/api/v1/b", "httpStatus": 200, "createdAt": stamp},
    ])
    first = client.get("/api/v1/request-events", params=_params(route="/api/v1/a", pageSize=1, cursor=""))
    assert first.status_code == 200
    cursor = first.json()["nextCursor"]
    assert cursor
    assert client.get("/api/v1/request-events", params=_params(route="/api/v1/b", pageSize=1, cursor=cursor)).status_code == 422
    counted = client.get("/api/v1/request-events", params=_params(route="/api/v1/a", pageSize=1, cursor=cursor, includeTotal="true"))
    assert counted.status_code == 200 and counted.json()["total"] == 2
    assert client.get("/api/v1/request-events", params={"cursor": "%%%"}).status_code == 422
    padded = cursor + "=" * (-len(cursor) % 4)
    malformed = json.loads(base64.urlsafe_b64decode(padded.encode()))
    malformed["unexpected"] = True
    unknown_field_cursor = base64.urlsafe_b64encode(
        json.dumps(malformed, separators=(",", ":")).encode(),
    ).decode().rstrip("=")
    assert client.get("/api/v1/request-events", params=_params(route="/api/v1/a", cursor=unknown_field_cursor)).status_code == 422


@pytest.mark.usefixtures("awaitable_mongomock_event_aggregate")
def test_runtime_cursor_keeps_legacy_detected_time_and_old_page_contract(client):
    """旧运行事件按 detectedAt 游标排序，未传游标仍返回原有页码响应。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 9, 14, 8, tzinfo=UTC)
    client.portal.call(repo.db.events.insert_many, [
        {"id": "legacy-new", "type": "CONNECTION_GAP", "detectedAt": stamp, "createdAt": None},
        {"id": "legacy-old", "type": "CONNECTION_GAP", "detectedAt": stamp - timedelta(seconds=1)},
        {"id": "modern", "type": "CONNECTION_GAP", "createdAt": stamp - timedelta(seconds=2)},
    ])
    old_page = client.get("/api/v1/runtime-events", params={"type": "CONNECTION_GAP", "pageSize": 1})
    assert old_page.status_code == 200
    assert {"total", "page", "pageSize", "items"}.issubset(old_page.json())
    assert "hasMore" not in old_page.json()

    first = client.get("/api/v1/runtime-events", params={"type": "CONNECTION_GAP", "pageSize": 2, "cursor": ""})
    assert first.status_code == 200, first.text
    body = first.json()
    assert [item["id"] for item in body["items"]] == ["legacy-new", "legacy-old"]
    assert body["hasMore"] is True and body["nextCursor"]
    second = client.get("/api/v1/runtime-events", params={"type": "CONNECTION_GAP", "pageSize": 2, "cursor": body["nextCursor"]})
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["items"]] == ["modern"]
