"""资源认证历史的正式查询、权限和健康 CAS 回归。"""

from datetime import UTC, datetime, timedelta

import pytest
from camera_logs.common.database import now
from camera_logs.resources.authentication_records import encode_cursor, record_authentication
from camera_logs.resources.health import _apply_success

pytest_plugins = ("test_resources",)


def _resource(identifier="camera"):
    """构造已认证海康资源，供历史记录和周期健康路径共同使用。"""
    return {"id": identifier, "name": "相机", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.8",
            "model": "old", "subSerialNumber": "old-serial", "authenticatedAt": now(),
            "healthStatus": "ONLINE", "deletedAt": None, "healthRevision": 1, "version": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [0, 7, 90])
async def test_authentication_expiry_respects_configured_days(resource_client, days):
    """零值仅对新写入记录关闭 TTL，正数精确计算过期时刻。"""
    repo = resource_client.app.state.repo
    repo.settings.authentication_record_retention_days = days
    timestamp = now().replace(microsecond=0)
    await record_authentication(repo, _resource(), source="PERIODIC", result="SUCCESS",
                                completed_at=timestamp)
    row = await repo.db.authentication_records.find_one({"resourceId": "camera"})
    if days == 0:
        assert "expiresAt" not in row
    else:
        assert row["expiresAt"].replace(tzinfo=timestamp.tzinfo) == timestamp + timedelta(days=days)


@pytest.mark.asyncio
async def test_late_success_is_not_hidden_by_a_later_fixed_bucket_start(resource_client):
    """较早完成但迟到的观测另起记录，仍能按它真实发生的时间检索。"""
    repo, resource = resource_client.app.state.repo, _resource()
    await repo.db.resources.insert_one(resource)
    stamp = now().replace(microsecond=0)
    for timestamp in (stamp, stamp - timedelta(minutes=10)):
        await record_authentication(repo, resource, source="PERIODIC", result="SUCCESS",
                                    before=resource, after=resource, completed_at=timestamp)
    response = resource_client.get("/api/v1/resources/camera/authentication-records", params={
        "start": (stamp - timedelta(minutes=11)).isoformat(),
        "end": (stamp - timedelta(minutes=9)).isoformat(),
    })
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["occurrenceCount"] == 1


def test_records_filter_intersection_and_stable_pagination(resource_client):
    """结果、身份变化和时间范围取交集，分页使用时间与 ID 的稳定降序。"""
    repo, resource = resource_client.app.state.repo, _resource()
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    stamp = now()
    records = [
        {"id": "a", "resourceId": "camera", "result": "SUCCESS", "identityChanged": True, "createdAt": stamp},
        {"id": "b", "resourceId": "camera", "result": "SUCCESS", "identityChanged": True, "createdAt": stamp},
        {"id": "c", "resourceId": "camera", "result": "AUTH_FAILED", "identityChanged": True, "createdAt": stamp},
        {"id": "d", "resourceId": "camera", "result": "SUCCESS", "identityChanged": False, "createdAt": stamp - timedelta(days=2)},
    ]
    resource_client.portal.call(repo.db.authentication_records.insert_many, records)
    query = {"result": "SUCCESS", "identityChanged": True, "start": (stamp-timedelta(minutes=1)).isoformat(),
             "end": (stamp+timedelta(minutes=1)).isoformat(), "pageSize": 1}
    first = resource_client.get("/api/v1/resources/camera/authentication-records", params=query)
    second = resource_client.get("/api/v1/resources/camera/authentication-records", params=query | {"page": 2})
    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == 2
    assert [first.json()["items"][0]["id"], second.json()["items"][0]["id"]] == ["b", "a"]


def test_authentication_history_range_intersects_aggregate_and_hides_expired(resource_client):
    """聚合状态跨进筛选范围仍可见；TTL 尚未调度时接口也不暴露过期历史。"""
    repo, resource = resource_client.app.state.repo, _resource()
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    stamp = now()
    records = [
        {"id": "aggregate", "resourceId": "camera", "result": "SUCCESS", "identityChanged": False,
         "createdAt": stamp - timedelta(days=2), "latestAt": stamp - timedelta(minutes=10),
         "expiresAt": stamp + timedelta(days=88)},
        {"id": "legacy", "resourceId": "camera", "result": "OFFLINE", "identityChanged": False,
         "createdAt": stamp - timedelta(minutes=20)},
        {"id": "outside", "resourceId": "camera", "result": "SUCCESS", "identityChanged": False,
         "createdAt": stamp - timedelta(days=2), "latestAt": stamp - timedelta(hours=2),
         "expiresAt": stamp + timedelta(days=88)},
        {"id": "expired", "resourceId": "camera", "result": "SUCCESS", "identityChanged": False,
         "createdAt": stamp - timedelta(days=100), "latestAt": stamp - timedelta(days=100),
         "expiresAt": stamp - timedelta(seconds=1)},
    ]
    resource_client.portal.call(repo.db.authentication_records.insert_many, records)
    response = resource_client.get("/api/v1/resources/camera/authentication-records", params={
        "start": (stamp - timedelta(minutes=30)).isoformat(), "end": (stamp - timedelta(minutes=5)).isoformat(),
    })
    assert response.status_code == 200
    assert {item["id"] for item in response.json()["items"]} == {"aggregate", "legacy"}


def test_authentication_history_retention_zero_keeps_legacy_records(resource_client):
    """管理员关闭认证历史保留时，查询不额外隐藏尚未被物理清理的旧记录。"""
    repo, resource = resource_client.app.state.repo, _resource()
    repo.settings.authentication_record_retention_days = 0
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    resource_client.portal.call(repo.db.authentication_records.insert_one, {
        "id": "old", "resourceId": "camera", "result": "SUCCESS", "identityChanged": False,
        "createdAt": now() - timedelta(days=100),
    })
    response = resource_client.get("/api/v1/resources/camera/authentication-records")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ["old"]


def test_authentication_history_hides_internal_revision_fields(resource_client):
    """并发审计修订号只供服务端校验，公开认证历史不得回传。"""
    repo, resource = resource_client.app.state.repo, _resource()
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    resource_client.portal.call(repo.db.authentication_records.insert_one, {
        "id": "internal-revisions", "resourceId": "camera", "result": "SUCCESS",
        "identityChanged": False, "createdAt": now(),
        "historyFirstRevision": 4, "historyLatestRevision": 7,
    })
    response = resource_client.get("/api/v1/resources/camera/authentication-records")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert "historyFirstRevision" not in item
    assert "historyLatestRevision" not in item


@pytest.mark.asyncio
async def test_initial_success_change_failure_and_stale_health_cas_records(resource_client):
    """初始、身份变化和失败均可追溯，迟到 CAS 不得写入额外历史。"""
    repo, resource = resource_client.app.state.repo, _resource()
    resource["authenticatedAt"] = None
    await repo.db.resources.insert_one(resource)
    await record_authentication(repo, resource, source="CREATE", result="SUCCESS", after=resource)
    before = resource | {"authenticatedAt": now(), "model": "old", "subSerialNumber": "old-serial"}
    await record_authentication(repo, before, source="EDIT", result="SUCCESS", before=before,
                                after=before | {"model": "new", "subSerialNumber": "new-serial"})
    await record_authentication(repo, before, source="EDIT", result="AUTH_FAILED", before=before, after=before)
    snapshot = before | {"healthLeaseToken": "stale", "healthRevision": 1}
    await _apply_success(repo, snapshot, {"model": "later", "subSerialNumber": "later"})
    rows = [row async for row in repo.db.authentication_records.find({"resourceId": "camera"})]
    assert [(row["result"], row["initialAuthentication"], row["identityChanged"]) for row in rows] == [
        ("SUCCESS", True, False), ("SUCCESS", False, True), ("AUTH_FAILED", False, False),
    ]


@pytest.mark.asyncio
async def test_consecutive_periodic_successes_merge_without_extending_ttl_or_crossing_failure(resource_client):
    """同日周期成功压缩为计数，失败和手动认证必须形成不可跨越的历史边界。"""
    repo, resource = resource_client.app.state.repo, _resource()
    repo.settings.authentication_record_retention_days = 90
    first = datetime(2026, 9, 11, 8, tzinfo=UTC)
    await record_authentication(repo, resource, source="PERIODIC", result="SUCCESS", before=resource,
                                after=resource, completed_at=first)
    await record_authentication(repo, resource, source="PERIODIC", result="SUCCESS", before=resource,
                                after=resource, completed_at=first + timedelta(hours=2))
    rows = [row async for row in repo.db.authentication_records.find({"resourceId": "camera"})]
    assert len(rows) == 1
    assert rows[0]["occurrenceCount"] == 2
    assert rows[0]["createdAt"].replace(tzinfo=UTC) == first
    assert rows[0]["latestAt"].replace(tzinfo=UTC) == first + timedelta(hours=2)
    assert rows[0]["expiresAt"].replace(tzinfo=UTC) == first + timedelta(days=90)

    await record_authentication(repo, resource, source="PERIODIC", result="OFFLINE", before=resource,
                                after=resource, completed_at=first + timedelta(hours=3))
    await record_authentication(repo, resource, source="PERIODIC", result="SUCCESS", before=resource,
                                after=resource, completed_at=first + timedelta(hours=4))
    await record_authentication(repo, resource, source="MANUAL", result="SUCCESS", before=resource,
                                after=resource, completed_at=first + timedelta(hours=5))
    await record_authentication(repo, resource, source="PERIODIC", result="SUCCESS", before=resource,
                                after=resource, completed_at=first + timedelta(hours=6))
    rows = [row async for row in repo.db.authentication_records.find({"resourceId": "camera"}).sort("createdAt", 1)]
    assert [(row["source"], row["result"], row["occurrenceCount"]) for row in rows] == [
        ("PERIODIC", "SUCCESS", 2), ("PERIODIC", "OFFLINE", 1),
        ("PERIODIC", "SUCCESS", 1), ("MANUAL", "SUCCESS", 1), ("PERIODIC", "SUCCESS", 1),
    ]


@pytest.mark.asyncio
async def test_periodic_success_on_next_utc_day_creates_new_fixed_ttl_bucket(resource_client):
    """UTC 跨日切分聚合桶，后续成功不可延长前一天记录的 TTL。"""
    repo, resource = resource_client.app.state.repo, _resource()
    first = datetime(2026, 9, 11, 23, 59, tzinfo=UTC)
    await record_authentication(repo, resource, source="PERIODIC", result="SUCCESS", before=resource,
                                after=resource, completed_at=first)
    await record_authentication(repo, resource, source="PERIODIC", result="SUCCESS", before=resource,
                                after=resource, completed_at=first + timedelta(minutes=2))
    rows = [row async for row in repo.db.authentication_records.find({"resourceId": "camera"}).sort("createdAt", 1)]
    assert len(rows) == 2
    assert [row["utcDay"] for row in rows] == ["2026-09-11", "2026-09-12"]
    assert all(row["occurrenceCount"] == 1 for row in rows)


def test_deleted_resource_remains_readable_and_reader_requires_authentication(resource_client):
    """软删保留历史给有效读者，匿名请求仍必须拒绝。"""
    repo, resource = resource_client.app.state.repo, _resource()
    resource["deletedAt"] = now()
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    resource_client.portal.call(repo.db.authentication_records.insert_one, {"id": "history", "resourceId": "camera", "result": "OFFLINE", "identityChanged": False, "createdAt": now()})
    assert resource_client.get("/api/v1/resources/camera/authentication-records").status_code == 200
    assert resource_client.get("/api/v1/resources/camera/authentication-records", headers={"Authorization": "Bearer bad"}).status_code == 401


def test_authentication_history_cursor_paginates_without_count(resource_client, monkeypatch):
    """长期认证历史使用排序键游标，跨相同时间戳不重复且不依赖全量计数。"""
    repo, resource = resource_client.app.state.repo, _resource()
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    stamp = now()
    records = [{"id": identifier, "resourceId": "camera", "result": "SUCCESS", "identityChanged": False,
                "createdAt": stamp} for identifier in ("c", "b", "a")]
    resource_client.portal.call(repo.db.authentication_records.insert_many, records)
    async def count_forbidden(*args, **kwargs):
        raise AssertionError("游标首屏和后续页不得进行全量计数")

    monkeypatch.setattr(repo.db.authentication_records, "count_documents", count_forbidden)
    first = resource_client.get("/api/v1/resources/camera/authentication-records", params={"pageSize": 2, "cursor": ""})
    assert first.status_code == 200
    payload = first.json()
    assert [item["id"] for item in payload["items"]] == ["c", "b"]
    assert payload["nextCursor"]
    assert payload["total"] is None
    assert payload["hasMore"] is True

    second = resource_client.get("/api/v1/resources/camera/authentication-records", params={
        "pageSize": 2, "cursor": payload["nextCursor"]})
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["items"]] == ["a"]
    assert second.json()["total"] is None
    assert second.json()["nextCursor"] is None


def test_authentication_history_rejects_foreign_or_malformed_cursor(resource_client):
    """游标必须绑定原资源且格式错误统一返回 422。"""
    repo, resource = resource_client.app.state.repo, _resource()
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    stamp = now()
    record = {"id": "a", "resourceId": "camera", "result": "SUCCESS", "identityChanged": False, "createdAt": stamp}
    resource_client.portal.call(repo.db.authentication_records.insert_one, record)
    foreign = encode_cursor(record | {"resourceId": "other"})
    assert resource_client.get("/api/v1/resources/camera/authentication-records", params={"cursor": foreign}).status_code == 422
    assert resource_client.get("/api/v1/resources/camera/authentication-records", params={"cursor": "%%%"}).status_code == 422
