"""用实际公开响应检验字段释义覆盖，未知嵌套字段不能被示例占位掩盖。"""

# ruff: noqa: F811
from datetime import timedelta
from types import SimpleNamespace

import pytest
from camera_logs.administration.settings import _public_discovered_node, _public_node_config
from camera_logs.common.api_contracts.response_admin import (
    DASHBOARD,
    DISPLAY_SETTINGS,
    IP_POLICY,
    PLATFORM_SETTINGS,
    TOKEN,
    TOKEN_SECRET,
)
from camera_logs.common.api_contracts.response_nodes import NODE, NODE_CONFIG
from camera_logs.common.database import now
from camera_logs.node.performance_runtime import update_performance
from camera_logs.node.telemetry import TelemetrySampler
from camera_logs.users.service_tokens import public_token
from test_api import client  # noqa: F401
from test_dashboard import _seed_dashboard_data, dashboard_client  # noqa: F401


def assert_documented(value, schema, path="$reponse"):
    """递归检查所有实际返回字段均有释义，动态字典也必须定义值合同。"""
    assert schema.get("description"), path
    if value is None:
        assert schema.get("nullable"), path
    elif isinstance(value, dict):
        properties = schema.get("properties", {})
        assert set(schema.get("required", [])) <= set(value), path
        for key, item in value.items():
            child = properties.get(key, schema.get("additionalProperties"))
            assert isinstance(child, dict), f"{path}.{key} 未解释"
            assert_documented(item, child, f"{path}.{key}")
    elif isinstance(value, list):
        assert "items" in schema, path
        for item in value:
            assert_documented(item, schema["items"], f"{path}[]")


@pytest.mark.parametrize(
    ("path", "schema"),
    [
        ("/api/v1/platform-settings", PLATFORM_SETTINGS),
        ("/api/v1/display-settings", DISPLAY_SETTINGS),
        ("/api/v1/admin/ip-policy", IP_POLICY),
    ],
)
def test_admin_configuration_actual_responses_have_complete_descriptions(client, path, schema):
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert_documented(response.json(), schema)


def test_dashboard_actual_nested_statistics_have_complete_descriptions(dashboard_client):
    _seed_dashboard_data(dashboard_client)
    response = dashboard_client.get(
        "/api/v1/dashboard?days=7&date=2026-10-12", headers={"Authorization": "Bearer dashboard-admin"}
    )
    assert response.status_code == 200, response.text
    assert_documented(response.json(), DASHBOARD)


@pytest.mark.asyncio
async def test_current_worker_runtime_and_configuration_fields_are_documented(client):
    """采样、预算与 API 公开投影用真实代码生成，覆盖未知/在线/无心跳三种形态。"""
    sample = TelemetrySampler().sample()
    worker = SimpleNamespace(telemetry=SimpleNamespace(value=sample, age_seconds=lambda: 0))
    latency = {
        "writeIoLatencyMs": 1,
        "writeIoLatencyP99Ms": 1,
        "writeIoLatencyPendingMs": 0,
        "writeIoLatencySamples": 1,
        "writeIoLatencyWindowSeconds": 60,
    }
    status = await update_performance(worker, {}, latency, 10)
    node = {
        "id": "contract-node",
        "url": "http://node",
        "capacity": 4,
        "activeTasks": 0,
        "claimVersion": 7,
        "heartbeat": now(),
        "telemetryAgeSeconds": 0,
        "telemetry": sample,
        "performanceStatus": status,
        "accepting": True,
        "diskPercent": 10,
        "shutdownToken": "contract-instance",
        "shutdownCompletedAt": now(),
        "plannedRestartRecoveryVersion": 2,
        "searchIndexCursor": "2026-10-20T12:00:00",
        "exportCleanupCursor": {"expiresAt": now(), "id": "contract-export"},
        **latency,
    }
    client.portal.call(client.app.state.repo.db.nodes.insert_one, node)
    response = client.get("/api/v1/nodes")
    assert response.status_code == 200, response.text
    assert_documented(response.json()["items"][0], NODE)
    documented = response.json()["items"][0]
    assert documented["claimVersion"] == 7
    assert documented["shutdownToken"] == "contract-instance"
    assert documented["plannedRestartRecoveryVersion"] == 2
    assert documented["exportCleanupCursor"]["id"] == "contract-export"
    config = {"id": node["id"], "url": "http://node", "capacity": 4, "accepting": True, "version": 1}
    assert_documented(_public_node_config(config, node), NODE_CONFIG)
    assert_documented(_public_node_config(config, None), NODE_CONFIG)
    assert_documented(_public_discovered_node(node), NODE_CONFIG)


def test_service_token_statuses_and_secret_result_are_documented():
    user = {"id": "user", "username": "account", "displayName": "账号", "enabled": True}
    token = {
        "id": "token",
        "name": "服务",
        "userId": "user",
        "version": 1,
        "revoked": False,
        "expiresAt": None,
        "createdAt": now(),
        "updatedAt": now(),
    }
    for document, owner in [
        (token, user),
        (token, None),
        (token | {"revoked": True}, user),
        (token | {"expiresAt": now() - timedelta(days=1)}, user),
        (token, user | {"enabled": False}),
        (token, user | {"deletedAt": now()}),
    ]:
        assert_documented(public_token(document, owner), TOKEN)
    assert_documented(public_token(token, user) | {"token": "test-placeholder"}, TOKEN_SECRET)
