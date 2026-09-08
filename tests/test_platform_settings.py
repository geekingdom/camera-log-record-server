"""验证平台配置的乐观锁、动态保留期和节点登记边界。"""

from camera_logs.common.database import now
from camera_logs.logs.maintenance import get_retention_days
from test_api import client  # noqa: F401


def test_platform_settings_have_default_version_and_detect_conflict(client):  # noqa: F811
    """管理员更新保留期必须携带当前版本，冲突不会覆盖较新的配置。"""
    initial = client.get("/api/v1/platform-settings")
    assert initial.status_code == 200, initial.text
    assert initial.json()["retentionDays"] == 7
    assert initial.json()["version"] == 1

    updated = client.patch("/api/v1/platform-settings", json={"retentionDays": 14, "version": 1})
    assert updated.status_code == 200, updated.text
    assert updated.json()["retentionDays"] == 14
    assert updated.json()["version"] == 2
    assert client.patch("/api/v1/platform-settings", json={"retentionDays": 30, "version": 1}).status_code == 409
    assert client.portal.call(client.app.state.repo.db.audit.count_documents,
                              {"action": "update_platform_settings", "targetId": "platform"}) == 1


def test_retention_uses_stored_platform_configuration(client):  # noqa: F811
    """维护任务每次读取数据库配置，而不是固定使用进程启动时的环境值。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.platform_settings.insert_one, {
        "id": "platform", "retentionDays": 3, "version": 1, "updatedAt": now(),
    })
    assert client.portal.call(get_retention_days, repo) == 3


def test_node_registration_does_not_create_heartbeat_and_merges_live_status(client):  # noqa: F811
    """登记仅保存允许配置；在线状态仍只由同 ID worker 的真实心跳决定。"""
    created = client.post("/api/v1/admin/nodes", json={
        "id": "edge-a", "url": "https://edge-a.example.test:8443", "capacity": 24, "accepting": True,
    })
    assert created.status_code == 201, created.text
    assert client.portal.call(client.app.state.repo.db.nodes.count_documents, {"id": "edge-a"}) == 0

    offline = client.get("/api/v1/admin/nodes")
    assert offline.status_code == 200, offline.text
    assert offline.json()["items"] == [{
        "id": "edge-a", "url": "https://edge-a.example.test:8443", "capacity": 24,
        "accepting": True, "version": 1, "registered": True, "online": False, "reportedAt": None,
    }]

    client.portal.call(client.app.state.repo.db.nodes.insert_one, {
        "id": "edge-a", "url": "https://worker.example.test:8443", "heartbeat": now(),
        "capacity": 1, "accepting": False,
    })
    online = client.get("/api/v1/admin/nodes").json()["items"][0]
    assert online["online"] is True
    assert online["reportedUrl"] == "https://worker.example.test:8443"
    assert online["capacity"] == 24

    edited = client.patch("/api/v1/admin/nodes/edge-a", json={
        "version": 1, "accepting": False, "capacity": 30,
    })
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2
    assert edited.json()["accepting"] is False


def test_unregistered_heartbeat_node_is_discoverable_without_creating_config(client):  # noqa: F811
    """已有 worker 心跳必须可见，但读取列表绝不能把它自动变成允许配置。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.nodes.insert_one, {
        "id": "existing-worker", "url": "https://worker.example.test:8001", "heartbeat": now(),
        "capacity": 8, "accepting": True, "activeTasks": 2,
    })

    response = client.get("/api/v1/admin/nodes")

    assert response.status_code == 200, response.text
    item = response.json()["items"]
    assert len(item) == 1
    assert item[0]["id"] == "existing-worker"
    assert item[0]["url"] == "https://worker.example.test:8001"
    assert item[0]["reportedUrl"] == "https://worker.example.test:8001"
    assert item[0]["capacity"] == 8
    assert item[0]["version"] == 0
    assert item[0]["registered"] is False
    assert item[0]["online"] is True
    assert client.portal.call(repo.db.node_configs.count_documents, {}) == 0


def test_node_registration_rejects_non_development_http_and_url_parts(client):  # noqa: F811
    """节点地址只允许 HTTPS 或本机开发 HTTP，且不能携带用户信息和路径。"""
    for url in ("http://node.example.test", "https://admin@node.example.test", "https://node.example.test/api"):
        response = client.post("/api/v1/admin/nodes", json={"id": url, "url": url, "capacity": 1})
        assert response.status_code == 422

    local = client.post("/api/v1/admin/nodes", json={
        "id": "local-node", "url": "http://127.0.0.1:8001", "capacity": 1,
    })
    assert local.status_code == 201, local.text
