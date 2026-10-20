"""以资源相关实际路由响应校验待整合的 API 响应合同。"""

from datetime import UTC, datetime, timedelta

import pytest
from camera_logs.common.database import now
from test_api import client as _api_client


@pytest.fixture(name="client")
def client_fixture(tmp_path):
    """复用正式 API 夹具，保证合同来源是路由而非合成文档示例。"""
    yield from _api_client.__wrapped__(tmp_path)


def _resolve(schema, catalog):
    """解析 API 目录中的命名响应 schema。"""
    while "$ref" in schema:
        schema = catalog["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def _assert_documented(value, schema, catalog, path="response"):
    """递归验证实际公开键均被待整合合同声明，且都有中文说明。"""
    schema = _resolve(schema, catalog)
    if value is None:
        assert schema.get("nullable"), f"{path} 返回 null 但合同未声明 nullable"
        return
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        assert properties, f"{path} 返回对象但合同没有 properties"
        assert set(schema.get("required", ())) <= value.keys(), f"{path} 缺少合同必需字段"
        for key, child in value.items():
            assert key in properties, f"{path}.{key} 缺少响应合同"
            assert properties[key].get("description"), f"{path}.{key} 缺少中文说明"
            _assert_documented(child, properties[key], catalog, f"{path}.{key}")
    elif isinstance(value, list):
        assert schema.get("items"), f"{path} 返回数组但合同没有 items"
        for index, child in enumerate(value):
            _assert_documented(child, schema["items"], catalog, f"{path}[{index}]")


def _assert_response(client, operation_id, response):
    """成功响应必须匹配服务实际发布的 API 目录映射。"""
    assert response.is_success, response.text
    catalog = client.get("/api/v1/api-reference").json()
    operation = next(item for item in catalog["operations"] if item["id"] == operation_id)
    _assert_documented(response.json(), operation["responseContract"]["schema"], catalog)


def test_resource_auth_metric_coredump_and_export_route_projections(client, monkeypatch):
    """创建、详情、游标、非空嵌套指标及导出各状态均只使用实际路由投影。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 10, 20, 12, tzinfo=UTC)
    resource = {
        "id": "resource-contract", "name": "合同相机", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.30",
        "username": "admin", "authType": "DIGEST", "model": "DS-2CD", "subSerialNumber": "SN-CONTRACT",
        "softwareVersion": "V5", "createdBy": "bootstrap", "createdByName": "管理员", "version": 3,
        "enableCoredumpMonitor": True, "enableResourceMonitor": True, "authenticatedAt": stamp,
        "healthStatus": "ONLINE", "healthCheckedAt": stamp, "healthFailureCount": 0,
        "nextHealthCheckAt": stamp + timedelta(minutes=1), "healthRevision": 2,
        "createdAt": stamp, "updatedAt": stamp, "deletedAt": None,
    }
    client.portal.call(repo.db.resources.insert_one, resource)
    client.portal.call(repo.db.tasks.insert_one, {
        "id": "resource-contract-task", "name": "摘要任务", "protocol": "SSH", "ip": "192.0.2.30",
        "port": 22, "status": "COLLECTING", "desiredState": "RUNNING", "resourceId": resource["id"],
        "createdBy": "bootstrap", "createdByName": "管理员", "serialServerResourceId": None,
    })
    _assert_response(client, "GET /api/v1/resources", client.get("/api/v1/resources"))
    _assert_response(client, "GET /api/v1/resources/{identifier}", client.get(f"/api/v1/resources/{resource['id']}"))

    from camera_logs.resources import batch_creation

    async def authenticated(index, item, _semaphore):
        return index, item, {"model": "DS-2CD", "subSerialNumber": "SN-BATCH", "softwareVersion": "V5"}, None

    monkeypatch.setattr(batch_creation, "_authenticate_item", authenticated)
    batch = client.post("/api/v1/resources/batch", headers={"Idempotency-Key": "resource-contract-batch"}, json={"items": [{
        "name": "批量相机", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.31", "username": "admin", "password": "secret",
    }]})
    _assert_response(client, "POST /api/v1/resources/batch", batch)
    assert "tasks" not in batch.json()["items"][0]

    client.portal.call(repo.db.authentication_records.insert_many, [{
        "id": f"auth-{index}", "resourceId": resource["id"], "createdAt": stamp - timedelta(minutes=index),
        "latestAt": stamp - timedelta(minutes=index), "occurrenceCount": index + 1,
        "source": "CREATE" if index == 0 else "PERIODIC", "result": "SUCCESS", "modelBefore": "DS-2CD",
        "modelAfter": "DS-2CD", "serialBefore": "SN-CONTRACT", "serialAfter": "SN-CONTRACT",
        "identityChanged": False, "initialAuthentication": index == 0, "message": None,
        "expiresAt": stamp + timedelta(days=90),
    } for index in range(2)])
    auth = client.get(f"/api/v1/resources/{resource['id']}/authentication-records", params={"cursor": "", "pageSize": 1})
    _assert_response(client, "GET /api/v1/resources/{identifier}/authentication-records", auth)
    assert auth.json()["hasMore"] is True and auth.json()["nextCursor"]
    normal_auth = client.get(f"/api/v1/resources/{resource['id']}/authentication-records", params={"pageSize": 1})
    _assert_response(client, "GET /api/v1/resources/{identifier}/authentication-records", normal_auth)
    assert normal_auth.json()["total"] == 2 and "hasMore" not in normal_auth.json()

    client.portal.call(repo.db.resource_metric_hours.insert_one, {
        "id": f"{resource['id']}:2026102012", "resourceId": resource["id"], "bucketStart": stamp,
        "expiresAt": stamp + timedelta(days=90), "samples": [{"minute": "202610201200", "sampledAt": stamp,
            "status": "PARTIAL", "configVersion": 2, "identity": {"model": "DS-2CD", "subSerialNumber": "SN-CONTRACT"},
            "values": [{"id": "mem", "name": "MemAvailable", "value": 128.5, "unit": "KB"}],
            "errors": [{"scope": "slab", "code": "METRIC_NOT_FOUND"}], "errorCode": "PARTIAL_FAILURE"}],
    })
    metrics = client.get(f"/api/v1/resources/{resource['id']}/resource-metrics", params={
        "start": "2026-10-20T19:00:00", "end": "2026-10-20T21:00:00",
    })
    _assert_response(client, "GET /api/v1/resources/{resource_id}/resource-metrics", metrics)
    sample = metrics.json()["items"][0]
    assert sample["values"] and sample["errors"] and sample["errorCode"] == "PARTIAL_FAILURE"

    inactive = client.get(f"/api/v1/resources/{resource['id']}/coredump-monitor")
    _assert_response(client, "GET /api/v1/resources/{identifier}/coredump-monitor", inactive)
    assert inactive.json() == {"active": False, "ownerTask": None, "mountStatus": None}
    current = now()
    client.portal.call(repo.db.resources.update_one, {"id": resource["id"]}, {"$set": {
        "coredumpLeaseTaskId": "resource-contract-task", "coredumpLeaseRunId": "monitor-run",
        "coredumpLeaseGeneration": 1, "coredumpLeaseNodeId": "monitor-node", "coredumpLeaseUntil": current + timedelta(seconds=60),
    }})
    client.portal.call(repo.db.tasks.update_one, {"id": "resource-contract-task"}, {"$set": {
        "runId": "monitor-run", "generation": 1, "nodeId": "monitor-node", "coredumpMountStatus": "MOUNTED",
    }})
    client.portal.call(repo.db.nodes.insert_one, {"id": "monitor-node", "heartbeat": current})
    active = client.get(f"/api/v1/resources/{resource['id']}/coredump-monitor")
    _assert_response(client, "GET /api/v1/resources/{identifier}/coredump-monitor", active)
    assert active.json()["active"] and active.json()["ownerTask"]["id"] == "resource-contract-task"
    _assert_response(client, "GET /api/v1/resources/{identifier}", client.get(f"/api/v1/resources/{resource['id']}"))

    core = {"id": "resource-contract-core", "resourceId": resource["id"], "nodeId": "test", "name": "core.contract",
            "kind": "COREDUMP", "status": "FROZEN", "size": 9, "receivedAt": stamp, "firstSeenAt": stamp,
            "sourceModifiedAt": stamp, "sourceState": "STABLE", "sourceObservedAt": stamp,
            "sourceUnchangedSince": stamp, "sourceStableAt": stamp, "version": 2, "createdAt": stamp,
            "updatedAt": stamp, "expiresAt": stamp + timedelta(hours=1), "startedAt": stamp,
            "completedAt": stamp, "filename": "core.contract", "bytes": 9, "etag": "etag", "error": None,
            "sourcePresent": True, "source": {"path": "/private/core.contract"}, "snapshot": {"path": "/private/frozen"}}
    client.portal.call(repo.db.coredump_files.insert_one, core)
    cores = client.get(f"/api/v1/resources/{resource['id']}/coredumps")
    _assert_response(client, "GET /api/v1/resources/{resource_id}/coredumps", cores)

    created = client.post("/api/v1/coredump-exports", headers={"Idempotency-Key": "resource-contract-export"},
                          json={"fileIds": [core["id"]]})
    _assert_response(client, "POST /api/v1/coredump-exports", created)
    export_id = created.json()["id"]
    for status, extra in (
        ("QUEUED", {}), ("RUNNING", {"updatedAt": stamp, "startedAt": stamp}),
        ("SUCCEEDED", {"updatedAt": stamp, "startedAt": stamp, "completedAt": stamp, "filename": "export.zip", "bytes": 9, "etag": "etag"}),
        ("FAILED", {"updatedAt": stamp, "startedAt": stamp, "completedAt": stamp, "error": "SNAPSHOT_FAILED"}),
        ("CANCELLED", {"updatedAt": stamp, "error": "CANCELLED_BY_USER"}),
    ):
        client.portal.call(repo.db.coredump_exports.update_one, {"id": export_id}, {"$set": {"status": status, **extra}})
        _assert_response(client, "GET /api/v1/coredump-exports/{identifier}", client.get(f"/api/v1/coredump-exports/{export_id}"))
