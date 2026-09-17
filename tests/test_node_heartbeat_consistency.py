"""验证管理端在线标记与节点健康对同一心跳时间边界给出一致结论。"""

from datetime import UTC, datetime, timedelta

from camera_logs.administration import settings
from camera_logs.node.health import measurements, node_health, rank_nodes
from test_api import client  # noqa: F401

CURRENT = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)


def test_received_heartbeat_ignores_eight_hour_worker_clock_offset(monkeypatch):
    """接收时间是唯一在线依据，正负八小时的节点时间只用于诊断。"""
    monkeypatch.setattr("camera_logs.node.health.now", lambda: CURRENT)
    for hours in (-8, 8):
        node = {"id": "remote", "heartbeat": CURRENT, "workerReportedAt": CURRENT + timedelta(hours=hours),
                "telemetryAgeSeconds": 1, "accepting": True, "capacity": 100, "diskPercent": 20,
                "writeLatencyMs": 1, "telemetry": {"sampledAt": CURRENT + timedelta(hours=hours),
                    "cpuPercent": 20, "memoryPercent": 30}}
        assert node_health(node, CURRENT)["status"] != "OFFLINE"
        assert measurements(node, CURRENT)["sampledAt"] == CURRENT - timedelta(seconds=1)
        assert rank_nodes([node], {})[0][1] == "remote"
        node["telemetry"]["cpuPercent"] = 99
        assert rank_nodes([node], {}) == []
        node["heartbeat"] = CURRENT - timedelta(seconds=31)
        assert node_health(node, CURRENT)["status"] == "OFFLINE"


def test_continuing_heartbeat_does_not_refresh_stuck_telemetry():
    """持续上报不能把停止采样的指标伪装成新样本，异常年龄同样按未知处理。"""
    for age in (16, None, -1, float("nan")):
        node = {"heartbeat": CURRENT, "telemetryAgeSeconds": age,
                "telemetry": {"sampledAt": CURRENT, "cpuPercent": 20}}
        assert measurements(node, CURRENT) == {}


def test_node_api_calibrates_telemetry_without_exposing_worker_clock_as_freshness(client, monkeypatch):  # noqa: F811
    """API和管理页均使用接收时刻，前端采样时刻已换算到平台时间。"""
    monkeypatch.setattr(settings, "now", lambda: CURRENT)
    monkeypatch.setattr("camera_logs.commands.api.now", lambda: CURRENT)
    repo = client.app.state.repo
    for hours in (-8, 8):
        client.portal.call(repo.db.nodes.insert_one, {
            "id": str(hours), "heartbeat": CURRENT, "workerReportedAt": CURRENT + timedelta(hours=hours),
            "telemetryAgeSeconds": 1, "accepting": True,
            "telemetry": {"sampledAt": CURRENT + timedelta(hours=hours), "cpuPercent": 20},
        })
    nodes = client.get("/api/v1/nodes").json()["items"]
    admin = client.get("/api/v1/admin/nodes").json()["items"]
    assert len(nodes) == len(admin) == 2
    assert all(item["online"] for item in admin)
    for item in nodes:
        assert item["health"]["status"] != "OFFLINE"
        assert datetime.fromisoformat(item["telemetry"]["sampledAt"]) == CURRENT - timedelta(seconds=1)
        assert item["telemetry"]["cpuPercent"] == 20


def test_admin_online_uses_the_health_freshness_window(monkeypatch):
    """管理配置页与节点健康都只接受当前到过去三十秒内的心跳。"""
    monkeypatch.setattr(settings, "now", lambda: CURRENT)

    assert settings._is_online({"heartbeat": CURRENT - timedelta(seconds=30)})
    assert not settings._is_online({"heartbeat": CURRENT - timedelta(seconds=30, milliseconds=1)})
    assert not settings._is_online({"heartbeat": CURRENT + timedelta(seconds=3)})


def test_node_health_explains_missing_invalid_stale_and_future_heartbeats():
    """直接写入的异常未来接收时间仍拒绝；正常Worker不再写入本机时间。"""
    assert node_health({}, CURRENT) == {"status": "OFFLINE", "reasons": ["未收到节点心跳"]}
    assert node_health({"heartbeat": "not-a-timestamp"}, CURRENT) == {
        "status": "OFFLINE", "reasons": ["节点心跳时间无效"],
    }
    assert node_health({"heartbeat": CURRENT - timedelta(seconds=31)}, CURRENT) == {
        "status": "OFFLINE", "reasons": ["超过30秒未收到节点心跳"],
    }
    assert node_health({"heartbeat": CURRENT + timedelta(seconds=3)}, CURRENT) == {
        "status": "OFFLINE", "reasons": ["心跳接收时间晚于API时间，请检查平台数据库时钟及Worker版本"],
    }


def test_node_apis_agree_on_future_and_fresh_heartbeats(client, monkeypatch):  # noqa: F811
    """节点视图与管理视图使用同一服务端心跳窗口，节点 API 返回 UTC 判定时刻。"""
    repo = client.app.state.repo
    timestamp = CURRENT
    monkeypatch.setattr(settings, "now", lambda: timestamp)
    monkeypatch.setattr("camera_logs.commands.api.now", lambda: timestamp)
    client.portal.call(repo.db.nodes.insert_many, [
        {"id": "future", "heartbeat": timestamp + timedelta(seconds=3), "accepting": True},
        {"id": "fresh", "heartbeat": timestamp, "accepting": True},
    ])

    nodes = {item["id"]: item for item in client.get("/api/v1/nodes").json()["items"]}
    admin = {item["id"]: item for item in client.get("/api/v1/admin/nodes").json()["items"]}

    assert nodes["future"]["health"] == {
        "status": "OFFLINE", "reasons": ["心跳接收时间晚于API时间，请检查平台数据库时钟及Worker版本"],
    }
    assert admin["future"]["online"] is False
    assert nodes["fresh"]["health"]["status"] != "OFFLINE"
    assert admin["fresh"]["online"] is True
    assessed_at = datetime.fromisoformat(nodes["fresh"]["assessedAt"])
    assert assessed_at.utcoffset() == timedelta(0)
