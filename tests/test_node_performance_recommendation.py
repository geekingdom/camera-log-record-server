"""逐节点性能推荐的确定性回归，不依赖执行机实际资源。"""

from datetime import UTC, datetime, timedelta

from camera_logs.administration.settings import _public_node_config
from camera_logs.node.performance_recommendation import performance_recommendation

MIB = 1024 * 1024
NOW = datetime(2026, 9, 20, tzinfo=UTC)


def node(*, cpu=4, available=1024 * MIB, source="HOST", heartbeat=NOW, sampled_at=NOW):
    """构造带新鲜心跳与资源探测的节点快照，单例只覆盖推荐所需字段。"""
    return {
        "id": "node", "heartbeat": heartbeat, "telemetry": {
            "sampledAt": sampled_at,
            "resourceLimits": {
                "effectiveCpu": cpu, "memoryLimitBytes": available * 2,
                "memoryAvailableBytes": available, "source": source,
            },
        },
    }


def test_recommendation_returns_conservative_weak_and_high_resource_policies():
    """弱节点降批量与读取，高配节点在合同上限内提高并发和读取。"""
    weak = performance_recommendation(node(cpu=1.8, available=512 * MIB), NOW)
    strong = performance_recommendation(node(cpu=12, available=8 * 1024 * MIB), NOW)

    assert weak["available"] is True
    assert weak["policy"] == {
        "mode": "AUTO", "bufferMiB": 32, "writeConcurrency": 1, "batchKiB": 64,
        "flushIntervalMs": 100, "readMiBPerSecond": 10,
    }
    assert strong["available"] is True
    assert strong["policy"] == {
        "mode": "AUTO", "bufferMiB": 256, "writeConcurrency": 8, "batchKiB": 256,
        "flushIntervalMs": 100, "readMiBPerSecond": 50,
    }


def test_recommendation_honors_cgroup_resources_and_rejects_insufficient_or_unknown_values():
    """容器实际可用量优先；未知CPU或不足64MiB时不伪造可保存建议。"""
    cgroup = performance_recommendation(node(cpu=.5, available=256 * MIB, source="CGROUP_V2"), NOW)
    unknown = performance_recommendation(node(cpu=0, available=256 * MIB), NOW)
    insufficient = performance_recommendation(node(available=63 * MIB), NOW)

    assert cgroup["policy"]["writeConcurrency"] == 1
    assert cgroup["policy"]["bufferMiB"] == 16
    assert cgroup["resources"]["source"] == "CGROUP_V2"
    for result in (unknown, insufficient):
        assert result["available"] is False
        assert result["policy"] is None and result["resources"] is None


def test_recommendation_rejects_stale_heartbeat_or_measurements():
    """过期接收心跳或单调采样窗口均不能产生建议。"""
    stale_heartbeat = performance_recommendation(node(heartbeat=NOW - timedelta(seconds=31)), NOW)
    stale_measurement = performance_recommendation(node(sampled_at=NOW - timedelta(seconds=16)), NOW)

    assert stale_heartbeat == {
        "available": False, "reason": "节点心跳已过期，无法推荐", "policy": None,
        "resources": None, "reportedAt": NOW - timedelta(seconds=31),
    }
    assert stale_measurement["available"] is False
    assert stale_measurement["reason"] == "节点遥测已过期，无法推荐"


def test_public_node_config_exposes_read_only_recommendation_without_changing_policy():
    """管理节点视图公开建议，但实际保存的策略仍完全来自管理员配置。"""
    config = {"id": "node", "url": "http://node", "capacity": 8, "accepting": True, "version": 1,
              "performance": {"mode": "MANUAL", "bufferMiB": 128, "writeConcurrency": 6,
                              "batchKiB": 256, "flushIntervalMs": 100, "readMiBPerSecond": 25}}

    public = _public_node_config(config, node(), NOW)

    assert public["performance"] == config["performance"]
    assert public["performanceRecommendation"]["available"] is True
    assert public["performanceRecommendation"]["policy"]["mode"] == "AUTO"
