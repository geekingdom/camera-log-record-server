"""验证管理端在线标记与节点健康对同一心跳时间边界给出一致结论。"""

from datetime import UTC, datetime, timedelta

from camera_logs.administration import settings
from camera_logs.node.health import node_health
from test_api import client  # noqa: F401

CURRENT = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)


def test_admin_online_uses_the_health_freshness_window(monkeypatch):
    """管理配置页与节点健康都只接受当前到过去三十秒内的心跳。"""
    monkeypatch.setattr(settings, "now", lambda: CURRENT)

    assert settings._is_online({"heartbeat": CURRENT - timedelta(seconds=30)})
    assert not settings._is_online({"heartbeat": CURRENT - timedelta(seconds=30, milliseconds=1)})
    assert not settings._is_online({"heartbeat": CURRENT + timedelta(seconds=3)})


def test_node_health_explains_missing_invalid_stale_and_future_heartbeats():
    """未来心跳必须提示 API 与 Worker 时钟不同步，不能伪称已经超时。"""
    assert node_health({}, CURRENT) == {"status": "OFFLINE", "reasons": ["未收到节点心跳"]}
    assert node_health({"heartbeat": "not-a-timestamp"}, CURRENT) == {
        "status": "OFFLINE", "reasons": ["节点心跳时间无效"],
    }
    assert node_health({"heartbeat": CURRENT - timedelta(seconds=31)}, CURRENT) == {
        "status": "OFFLINE", "reasons": ["超过30秒未收到节点心跳"],
    }
    assert node_health({"heartbeat": CURRENT + timedelta(seconds=3)}, CURRENT) == {
        "status": "OFFLINE", "reasons": ["节点心跳时间晚于API时间超过2秒，API与Worker时钟不同步"],
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
        "status": "OFFLINE", "reasons": ["节点心跳时间晚于API时间超过2秒，API与Worker时钟不同步"],
    }
    assert admin["future"]["online"] is False
    assert nodes["fresh"]["health"]["status"] != "OFFLINE"
    assert admin["fresh"]["online"] is True
    assessed_at = datetime.fromisoformat(nodes["fresh"]["assessedAt"])
    assert assessed_at.utcoffset() == timedelta(0)
