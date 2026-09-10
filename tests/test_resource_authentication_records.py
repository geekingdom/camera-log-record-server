"""资源认证历史的正式查询、权限和健康 CAS 回归。"""

from datetime import timedelta

import pytest
from camera_logs.common.database import now
from camera_logs.resources.authentication_records import record_authentication
from camera_logs.resources.health import _apply_success

pytest_plugins = ("test_resources",)


def _resource(identifier="camera"):
    """构造已认证海康资源，供历史记录和周期健康路径共同使用。"""
    return {"id": identifier, "name": "相机", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.8",
            "model": "old", "subSerialNumber": "old-serial", "authenticatedAt": now(),
            "healthStatus": "ONLINE", "deletedAt": None, "healthRevision": 1, "version": 1}


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


def test_deleted_resource_remains_readable_and_reader_requires_authentication(resource_client):
    """软删保留历史给有效读者，匿名请求仍必须拒绝。"""
    repo, resource = resource_client.app.state.repo, _resource()
    resource["deletedAt"] = now()
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    resource_client.portal.call(repo.db.authentication_records.insert_one, {"id": "history", "resourceId": "camera", "result": "OFFLINE", "identityChanged": False, "createdAt": now()})
    assert resource_client.get("/api/v1/resources/camera/authentication-records").status_code == 200
    assert resource_client.get("/api/v1/resources/camera/authentication-records", headers={"Authorization": "Bearer bad"}).status_code == 401
