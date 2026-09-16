"""请求对象只取路由模板和允许的路径标识，不读取正文或查询中的凭据。"""

from camera_logs.common.request_targets import request_target

pytest_plugins = ("test_api",)


def test_collection_and_platform_requests_have_meaningful_targets():
    assert request_target("/api/v1/nodes", {}) == {
        "targetKind": "nodes", "targetLabel": "服务节点", "targetScope": "COLLECTION",
    }
    assert request_target("/api/v1/platform-settings", {})["targetScope"] == "PLATFORM"
    assert request_target("/api/v1/display-settings", {}) == {
        "targetKind": "display-settings", "targetLabel": "实时日志显示配置", "targetScope": "PLATFORM",
    }


def test_named_and_generic_path_ids_are_bound_to_the_route_object():
    assert request_target("/api/v1/resources/{identifier}", {"identifier": "device-1"}) == {
        "targetKind": "resources", "targetLabel": "设备资源", "targetScope": "OBJECT",
        "targetId": "device-1", "resourceId": "device-1",
    }
    assert request_target("/api/v1/admin/nodes/{node_id}", {"node_id": "worker-b"})["nodeId"] == "worker-b"
    assert request_target("/api/v1/tasks/{task_id}/start", {"task_id": "task-1"})["taskId"] == "task-1"
    assert "targetId" not in request_target("/api/v1/resources/authenticate", {"password": "secret"})


def test_requests_persist_resource_node_and_collection_targets(client):
    for path, identifier in [("/api/v1/resources/fixture-device", "fixture-device"),
                             ("/api/v1/nodes", None), ("/api/v1/admin/nodes/missing", "missing")]:
        response = client.get(path) if identifier != "missing" else client.patch(path, json={"version": 1})
        event = client.portal.call(client.app.state.repo.db.request_events.find_one,
                                   {"requestId": response.headers["x-request-id"]})
        assert event.get("targetId") == identifier
        assert event["targetLabel"] in {"服务节点", "设备资源"}
        listed = client.get("/api/v1/request-events", params={"requestId": event["requestId"]}).json()["items"][0]
        assert listed["targetLabel"] == event["targetLabel"]
        if identifier == "fixture-device":
            assert listed["targetName"] == "测试设备"


def test_old_collection_records_are_presented_without_inventing_missing_ids(client):
    client.portal.call(client.app.state.repo.db.request_events.insert_one, {
        "requestId": "old-nodes", "route": "/api/v1/nodes", "method": "GET",
    })
    item = client.get("/api/v1/request-events", params={"requestId": "old-nodes"}).json()["items"][0]
    assert item["targetLabel"] == "服务节点"
    assert item["targetScope"] == "COLLECTION"
    assert not item.get("targetId")
