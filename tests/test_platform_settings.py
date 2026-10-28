"""验证平台配置的乐观锁、动态保留期和节点登记边界。"""

from camera_logs.common.database import now
from camera_logs.common.security import actor
from camera_logs.logs.maintenance import get_retention_days
from test_api import client  # noqa: F401


def test_platform_settings_have_default_version_and_detect_conflict(client):  # noqa: F811
    """管理员更新保留期必须携带当前版本，冲突不会覆盖较新的配置。"""
    initial = client.get("/api/v1/platform-settings")
    assert initial.status_code == 200, initial.text
    assert initial.json()["retentionDays"] == 7
    assert initial.json()["clusterCapacity"] == 500
    assert initial.json()["liveLogBufferMiB"] == 10
    assert initial.json()["serviceLogRetentionDays"] == 7
    assert initial.json()["logSegmentMiB"] == 10
    assert initial.json()["version"] == 1

    updated = client.patch("/api/v1/platform-settings", json={"retentionDays": 14, "version": 1})
    assert updated.status_code == 200, updated.text
    assert updated.json()["retentionDays"] == 14
    assert updated.json()["version"] == 2
    assert updated.json()["clusterCapacity"] == 500
    assert client.patch("/api/v1/platform-settings", json={"retentionDays": 30, "version": 1}).status_code == 409
    assert client.portal.call(client.app.state.repo.db.audit.count_documents,
                              {"action": "update_platform_settings", "targetId": "platform"}) == 1


def test_log_segment_size_is_versioned_strict_and_preserved_when_omitted(client):  # noqa: F811
    """日志片段接受常规档位和512端点，旧配置读取默认为十且局部编辑不重置。"""
    initial = client.get("/api/v1/platform-settings").json()
    changed = client.patch("/api/v1/platform-settings", json={
        "retentionDays": initial["retentionDays"], "logSegmentMiB": 25, "version": initial["version"],
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["logSegmentMiB"] == 25
    preserved = client.patch("/api/v1/platform-settings", json={
        "retentionDays": 8, "version": changed.json()["version"],
    })
    assert preserved.status_code == 200, preserved.text
    assert preserved.json()["logSegmentMiB"] == 25
    endpoint = client.patch("/api/v1/platform-settings", json={
        "retentionDays": 8, "logSegmentMiB": 512, "version": preserved.json()["version"],
    })
    assert endpoint.status_code == 200, endpoint.text
    assert endpoint.json()["logSegmentMiB"] == 512
    for invalid in (9, 11, 511, 513, 12.5, True):
        response = client.patch("/api/v1/platform-settings", json={
            "retentionDays": 8, "logSegmentMiB": invalid, "version": endpoint.json()["version"],
        })
        assert response.status_code == 422


def test_legacy_platform_settings_default_log_segment_size_without_persisting_on_get(client):  # noqa: F811
    """历史平台记录缺少新字段时公开默认值，读取不把兼容默认误写为用户修改。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.platform_settings.insert_one, {
        "id": "platform", "retentionDays": 7, "version": 1, "updatedAt": now(),
    })
    response = client.get("/api/v1/platform-settings")
    assert response.status_code == 200, response.text
    assert response.json()["logSegmentMiB"] == 10
    stored = client.portal.call(repo.db.platform_settings.find_one, {"id": "platform"})
    assert "logSegmentMiB" not in stored


def test_platform_settings_normalizes_invalid_historical_log_segment_size(client):  # noqa: F811
    """管理端展示必须与运行时对历史损坏分片大小的安全回退保持一致。"""
    repo = client.app.state.repo
    for value in (None, 9, 11, 511, 513, True, "25"):
        client.portal.call(repo.db.platform_settings.delete_many, {})
        client.portal.call(repo.db.platform_settings.insert_one, {
            "id": "platform", "retentionDays": 7, "logSegmentMiB": value,
            "version": 1, "updatedAt": now(),
        })
        response = client.get("/api/v1/platform-settings")
        assert response.status_code == 200, response.text
        assert response.json()["logSegmentMiB"] == 10


def test_service_log_retention_is_versioned_validated_and_applies_to_api_process(client, monkeypatch):  # noqa: F811
    """运行日志保留期独立于设备日志，保存后立即通知当前 API 进程的日志处理器。"""
    from camera_logs.administration import settings as settings_module
    applied = []
    async def apply(retention_days):
        applied.append(retention_days)
    monkeypatch.setattr(settings_module, "apply_service_log_retention_days", apply)
    initial = client.get("/api/v1/platform-settings").json()
    changed = client.patch("/api/v1/platform-settings", json={
        "retentionDays": initial["retentionDays"], "serviceLogRetentionDays": 21,
        "version": initial["version"],
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["serviceLogRetentionDays"] == 21
    assert applied == [21]
    preserved = client.patch("/api/v1/platform-settings", json={
        "retentionDays": 8, "version": changed.json()["version"],
    })
    assert preserved.json()["serviceLogRetentionDays"] == 21
    for invalid in (0, 3651, True, 1.5):
        response = client.patch("/api/v1/platform-settings", json={
            "retentionDays": 8, "serviceLogRetentionDays": invalid,
            "version": preserved.json()["version"],
        })
        assert response.status_code == 422


def test_live_log_buffer_setting_is_versioned_and_display_api_is_minimal(client):  # noqa: F811
    """管理员保存浏览器日志内存上限，普通已登录用户只读取这一项显示配置。"""
    initial = client.get("/api/v1/platform-settings").json()
    changed = client.patch("/api/v1/platform-settings", json={
        "retentionDays": initial["retentionDays"], "version": initial["version"], "liveLogBufferMiB": 24,
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["liveLogBufferMiB"] == 24
    assert client.get("/api/v1/display-settings").json() == {"liveLogBufferMiB": 24}
    for invalid in (0, 101, True, 1.5):
        rejected = client.patch("/api/v1/platform-settings", json={
            "retentionDays": initial["retentionDays"], "version": changed.json()["version"],
            "liveLogBufferMiB": invalid,
        })
        assert rejected.status_code == 422

    client.app.dependency_overrides[actor] = lambda: {
        "id": "ordinary-user", "scopes": ["tasks:read"], "isAdmin": False,
    }
    try:
        assert client.get("/api/v1/display-settings").json() == {"liveLogBufferMiB": 24}
        assert client.get("/api/v1/platform-settings").status_code == 403
    finally:
        client.app.dependency_overrides.pop(actor, None)


def test_platform_and_node_capacity_accept_values_above_100(client):  # noqa: F811
    """节点和集群容量不再被历史 100 上限截断，保存值也会在调度配置中返回。"""
    node = client.post("/api/v1/admin/nodes", json={
        "id": "large-node", "url": "http://large-node:8001", "capacity": 250,
    })
    assert node.status_code == 201, node.text
    assert node.json()["capacity"] == 250
    updated = client.patch("/api/v1/platform-settings", json={
        "retentionDays": 7, "clusterCapacity": 1200, "version": 1,
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["clusterCapacity"] == 1200
    assert client.get("/api/v1/platform-settings").json()["clusterCapacity"] == 1200


def test_growth_retention_is_versioned_validated_and_preserved(client):  # noqa: F811
    """单独保存其它设置不覆盖记录保留策略，零值关闭且非法天数拒绝保存。"""
    current = client.get("/api/v1/platform-settings").json()
    assert current["recordRetention"] == {"auditDays": 90, "eventDays": 90, "runDays": 90}
    policy = {"auditDays": 0, "eventDays": 30, "runDays": 180}
    response = client.patch("/api/v1/platform-settings", json={
        "retentionDays": 7, "version": current["version"], "recordRetention": policy})
    assert response.status_code == 200, response.text
    updated = client.patch("/api/v1/platform-settings", json={"retentionDays": 8, "version": response.json()["version"]})
    assert updated.json()["recordRetention"] == policy
    for bad in (-1, 3651, True, 1.5):
        assert client.patch("/api/v1/platform-settings", json={
            "retentionDays": 8, "version": updated.json()["version"],
            "recordRetention": {"auditDays": bad}}).status_code == 422


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
        "isGeneralNode": True, "resourceNetworks": [],
        "inputRateLimitMiB": 50,
        "writeLatencyLimitMs": 200,
        "performance": {"mode": "AUTO", "bufferMiB": 64, "writeConcurrency": 4, "batchKiB": 256,
                        "flushIntervalMs": 100, "readMiBPerSecond": 50},
        "performanceRecommendation": {
            "available": False, "reason": "节点心跳已过期，无法推荐", "policy": None,
            "resources": None, "reportedAt": None,
        },
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


def test_node_performance_policy_defaults_replaces_as_a_whole_and_exposes_heartbeat_status(client):  # noqa: F811
    """策略默认值兼容旧配置；PATCH 省略保留，传入对象按完整新策略替换。"""
    created = client.post("/api/v1/admin/nodes", json={
        "id": "performance-node", "url": "http://performance-node:8001", "capacity": 8,
    })
    assert created.status_code == 201, created.text
    default = {"mode": "AUTO", "bufferMiB": 64, "writeConcurrency": 4, "batchKiB": 256,
               "flushIntervalMs": 100, "readMiBPerSecond": 50}
    assert created.json()["performance"] == default
    unchanged = client.patch("/api/v1/admin/nodes/performance-node", json={"version": 1, "capacity": 9})
    assert unchanged.status_code == 200 and unchanged.json()["performance"] == default
    replacement = default | {"mode": "MANUAL", "bufferMiB": 128, "writeConcurrency": 6}
    updated = client.patch("/api/v1/admin/nodes/performance-node", json={"version": 2, "performance": replacement})
    assert updated.status_code == 200 and updated.json()["performance"] == replacement
    client.portal.call(client.app.state.repo.db.nodes.insert_one, {
        "id": "performance-node", "url": "http://performance-node:8001", "heartbeat": now(),
        "performanceStatus": {"state": "MANUAL", "effective": {"bufferMiB": 128}, "reason": "手动预算"},
    })
    item = next(value for value in client.get("/api/v1/admin/nodes").json()["items"] if value["id"] == "performance-node")
    assert item["performanceStatus"] == {"state": "MANUAL", "effective": {"bufferMiB": 128}, "reason": "手动预算"}


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


def test_node_registration_rejects_invalid_url_parts(client):  # noqa: F811
    """部署支持 HTTP(S)，但节点公布地址不能携带凭据、脚本或附加路径。"""
    for url in ("ftp://node.example.test", "https://admin@node.example.test", "https://node.example.test/api",
                "http://0.0.0.0:8001", "http://[::]:8001", "http://worker:8001?x=1", "http://worker:8001#x",
                "http://worker:99999", "[http://worker:8001](http://worker:8001)"):
        response = client.post("/api/v1/admin/nodes", json={"id": url, "url": url, "capacity": 1})
        assert response.status_code == 422

    local = client.post("/api/v1/admin/nodes", json={
        "id": "local-node", "url": "http://127.0.0.1:8001", "capacity": 1,
    })
    assert local.status_code == 201, local.text


def test_http_deployment_node_can_be_registered_without_losing_heartbeat(client):  # noqa: F811
    """完整 Docker、内网独立部署及 IPv6 节点都允许按真实上报地址登记。"""
    repo = client.app.state.repo
    for identifier, url in (("compose-worker-1", "http://worker:8001"),
                            ("collector-01", "http://10.41.203.43:8001"),
                            ("ipv6-node", "http://[fd00::43]:8001")):
        client.portal.call(repo.db.nodes.insert_one, {
            "id": identifier, "url": url, "heartbeat": now(), "capacity": 100, "accepting": True,
        })
        response = client.post("/api/v1/admin/nodes", json={"id": identifier, "url": url, "capacity": 50})
        assert response.status_code == 201, response.text
        assert response.json()["online"] is True
        assert response.json()["reportedUrl"] == url
        assert response.json()["urlMismatch"] is False
        edited = client.patch(f"/api/v1/admin/nodes/{identifier}", json={"version": 1, "capacity": 60})
        assert edited.status_code == 200 and edited.json()["online"] is True
