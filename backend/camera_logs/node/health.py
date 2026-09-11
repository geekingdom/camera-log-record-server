"""统一解释节点健康与调度成本；未知指标不能伪装为空闲，压力只限制新任务。"""

import math
from datetime import UTC, datetime

from camera_logs.common.config import DEFAULT_NODE_CAPACITY
from camera_logs.common.database import now
from camera_logs.node.resource_routing import accepts_resource


def number(value):
    """仅接收非负有限测量值，拒绝布尔、NaN及重置产生的负数。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def fresh(timestamp, current, seconds=15):
    """心跳和采样采用服务端UTC；未来过远或格式错误的时间同样视为未知。"""
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp)
        except ValueError:
            return False
    if not isinstance(timestamp, datetime):
        return False
    age = (current - timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else current - timestamp).total_seconds()
    return -2 <= age <= seconds


def measurements(node, current=None):
    """只使用最近15秒采样参与健康与调度；旧指标不能长期否决节点或假装健康。"""
    sample = node.get("telemetry") or {}
    return sample if fresh(sample.get("sampledAt"), current or now()) else {}


def resource_pressure(node, current=None):
    """新鲜CPU或内存达到95%时暂停新任务，不主动断开已有连接。"""
    sample = measurements(node, current)
    return any(number(sample.get(key)) is not None and sample[key] >= 95 for key in ("cpuPercent", "memoryPercent"))


def node_health(node, current=None):
    """由真实指标给出健康状态及可排查原因，离线覆盖历史健康快照。"""
    current = current or now()
    if not fresh(node.get("heartbeat"), current, 30):
        return {"status": "OFFLINE", "reasons": ["超过30秒未收到节点心跳"]}
    critical, warnings, unknown = [], [], []
    if node.get("isolated"):
        critical.append("节点已隔离")
    if node.get("configurationMismatch"):
        critical.append("节点地址配置与运行地址不一致")
    sample = measurements(node, current)
    for key, label, warning, limit, source in (
        ("cpuPercent", "CPU使用率", 80, 95, sample),
        ("memoryPercent", "内存使用率", 85, 95, sample),
        ("diskPercent", "磁盘使用率", 80, 90, node),
        ("writeLatencyMs", "写入延迟", 150, 200, node),
    ):
        value = number(source.get(key))
        if value is None:
            unknown.append(f"{label}暂无有效数据")
        elif (value > 200 if key == "writeLatencyMs" else value >= limit):
            critical.append(f"{label}过高（{value:.1f}{'ms' if key == 'writeLatencyMs' else '%'}）")
        elif value >= warning:
            warnings.append(f"{label}接近限值（{value:.1f}{'ms' if key == 'writeLatencyMs' else '%'}）")
    if not sample:
        unknown.append("服务器指标未采样或已超过15秒")
    elif any(number(sample.get(key)) is None for key in ("networkUploadBytesPerSecond", "networkDownloadBytesPerSecond")):
        unknown.append("网络速率尚未形成连续采样")
    capacity = number(node.get("capacity")) or DEFAULT_NODE_CAPACITY
    if (number(node.get("activeTasks")) or 0) >= capacity:
        warnings.append("任务容量已满")
    if not node.get("accepting", False):
        warnings.append("节点当前不接收新任务")
    status = "CRITICAL" if critical else "WARNING" if warnings else "UNKNOWN" if unknown else "HEALTHY"
    return {"status": status, "reasons": critical + warnings + unknown}


def rank_nodes(nodes, occupancy, task=None):
    """先检查硬准入，再按容量/CPU/内存/磁盘/写延迟/网络的加权成本排序。

    网络按本轮候选最大上下行总速率归一，避免假定网卡带宽。新鲜指标缺失时使用
    保守成本；旧节点仍可回退接收，但不会因为缺少测量被误认为零负载。
    """
    current, candidates = now(), []
    for node in nodes:
        if task is not None and not accepts_resource(node, task.get("resourceIp", task.get("ip"))):
            continue
        if task and task.get("requiresNfs") and not (node.get("capabilities") or {}).get("coredumpNfs"):
            continue
        capacity = number(node.get("capacity", DEFAULT_NODE_CAPACITY))
        if capacity is None or capacity <= 0:
            continue
        count = occupancy.get(node["id"], 0)
        if not fresh(node.get("heartbeat"), current) or not node.get("accepting") or node.get("isolated") \
                or node.get("deletedAt") or node.get("configurationMismatch") or count >= capacity \
                or (number(node.get("diskPercent")) or 0) >= 90 or (number(node.get("writeLatencyMs")) or 0) > 200 \
                or resource_pressure(node, current):
            continue
        sample = measurements(node, current)
        upload, download = (number(sample.get(key)) for key in ("networkUploadBytesPerSecond", "networkDownloadBytesPerSecond"))
        network = upload + download if upload is not None and download is not None else None
        candidates.append((node, count / capacity, sample, network))
    peak_network = max([value[3] or 0 for value in candidates] + [1])
    ranked = []
    for node, utilisation, sample, network in candidates:
        cpu, memory = (number(sample.get(key)) for key in ("cpuPercent", "memoryPercent"))
        score = (.30 * utilisation + .20 * (cpu / 100 if cpu is not None else .75)
                 + .20 * (memory / 100 if memory is not None else .75)
                 + .10 * ((number(node.get("diskPercent")) or 0) / 100)
                 + .10 * min(1, (number(node.get("writeLatencyMs")) or 0) / 200)
                 + .10 * (network / peak_network if network is not None else .75))
        # 健康专用节点优先；同一类别仍使用原加权成本，硬准入失败已在上面剔除。
        priority = 2 if task is not None and node.get("isGeneralNode", True) else 0
        ranked.append((priority + score, node["id"]))
    return sorted(ranked)
