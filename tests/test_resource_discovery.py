"""第三方资源发现、组合过滤及关联任务摘要的权限回归。"""
# ruff: noqa: F811
from test_resources import resource_client  # noqa: F401


def test_resource_identity_filters_and_task_summaries(resource_client):
    repo = resource_client.app.state.repo
    resource_client.portal.call(repo.db.resources.insert_many, [
        {"id": "camera", "name": "大厅摄像机", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.10",
         "model": "DS-2CD", "subSerialNumber": "SN-001", "softwareVersion": "V5.10 build 260612"},
        {"id": "other", "name": "其他", "kind": "SERIAL_SERVER", "ip": "192.0.2.11"},
    ])
    resource_client.portal.call(repo.db.tasks.insert_many, [
        {"id": "a", "name": "SSH采集", "protocol": "SSH", "resourceId": "camera", "ip": "192.0.2.10",
         "port": 22, "status": "STOPPED", "desiredState": "STOPPED", "passwordEncrypted": "hidden"},
        {"id": "b", "protocol": "TELNET_SERIAL", "resourceId": "camera", "serialServerResourceId": "other"},
    ])
    for params in ({"search": "SN-001"}, {"search": "260612"}, {"model": "DS-2CD"},
                   {"name": "大厅", "ip": "192.0.2.10", "subSerialNumber": "SN-001"}):
        response = resource_client.get("/api/v1/resources", params=params)
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == ["camera"]
    limited = resource_client.get("/api/v1/resources/camera?taskLimit=1").json()
    assert limited["taskCount"] == 2 and limited["tasksTruncated"] is True
    assert limited["tasks"][0]["id"] == "a"
    assert limited["tasks"][0]["protocol"] == "SSH"
    assert "passwordEncrypted" not in limited["tasks"][0]
    assert resource_client.get("/api/v1/resources?name=其他&model=DS-2CD").json()["total"] == 0


def test_resources_filter_by_creator_without_relaxing_other_filters(resource_client):
    """创建人筛选是精确条件，仍应与软删除和名称等现有条件取交集。"""
    repo = resource_client.app.state.repo
    resource_client.portal.call(repo.db.resources.insert_many, [
        {"id": "owner-active", "name": "相机", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.31", "createdBy": "owner"},
        {"id": "other-active", "name": "相机", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.32", "createdBy": "other"},
        {"id": "owner-deleted", "name": "相机", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.33", "createdBy": "owner", "deletedAt": "2026-09-09T00:00:00Z"},
    ])

    response = resource_client.get("/api/v1/resources", params={"createdBy": "owner", "name": "相机"})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ["owner-active"]

    archived = resource_client.get("/api/v1/resources", params={"createdBy": "owner", "includeDeleted": "true"})
    assert {item["id"] for item in archived.json()["items"]} == {"owner-active", "owner-deleted"}


def test_user_bound_service_token_sees_all_tasks_in_shared_resource(resource_client):
    repo = resource_client.app.state.repo
    resource_client.portal.call(repo.db.resources.insert_one, {"id": "shared", "name": "共享资源"})
    resource_client.portal.call(repo.db.tasks.insert_many, [
        {"id": "allowed", "resourceId": "shared", "protocol": "SSH"},
        {"id": "secret", "resourceId": "shared", "protocol": "TELNET_SERIAL"},
    ])
    user = resource_client.post("/api/v1/users", json={
        "username": "resource-reader", "displayName": "资源查询用户", "password": "example-password-123", "scopes": [],
    }).json()
    token = resource_client.post("/api/v1/service-tokens", json={
        "name": "第三方只读", "userId": user["id"],
    }).json()["token"]
    result = resource_client.get("/api/v1/resources", headers={"Authorization": "Bearer " + token}).json()
    resource = result["items"][0]
    assert resource["taskCount"] == 2
    assert [task["id"] for task in resource["tasks"]] == ["allowed", "secret"]
    assert resource["tasksTruncated"] is False
