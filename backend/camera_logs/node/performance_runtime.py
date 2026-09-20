"""把节点性能决策应用到采集、写入和后台读取，管理员阈值保持独立。"""

import time

from camera_logs.collection import buffering

_effective = {"readMiBPerSecond": 50}
_pressured = False
_archives = {}


def background_settings():
    """压缩作业实际执行时读取后台预算，不在原子发布中途强行取消。"""
    return _effective["readMiBPerSecond"] * 1024 * 1024, not _pressured


def archive_started(token):
    """只保留活动归档的开始时刻，完成后删除，不增加数据库历史集合。"""
    _archives[token] = {"queuedAt": time.monotonic(), "runningAt": None}


def archive_running(token):
    """只有获得压缩执行槽后才计入处理中，避免把进程队列当作磁盘耗时。"""
    if token in _archives:
        _archives[token]["runningAt"] = time.monotonic()


def archive_finished(token):
    """无论成功失败均结束当前等待计时，失败仍由既有归档恢复流程处理。"""
    _archives.pop(token, None)


def archive_snapshot():
    """分别报告排队和执行时间；总耗时保留以兼容旧节点看板的告警合同。"""
    tick = time.monotonic()
    waiting = [item["queuedAt"] for item in _archives.values() if item["runningAt"] is None]
    running = [item["runningAt"] for item in _archives.values() if item["runningAt"] is not None]

    def oldest(values):
        return max(0, tick - min(values)) if values else 0

    return {"pendingJobs": len(_archives), "waitingJobs": len(waiting), "runningJobs": len(running),
            "oldestSeconds": oldest([item["queuedAt"] for item in _archives.values()]),
            "oldestWaitingSeconds": oldest(waiting), "oldestRunningSeconds": oldest(running)}


async def update_performance(worker, config, write_latency, disk_percent):
    """心跳周期应用有界决策；指标采样缺失时由控制器保守处理。"""
    global _effective, _pressured
    from camera_logs.logs import write_io
    from camera_logs.logs.archive_access import read_limiter
    from camera_logs.node.performance import PerformanceController, PerformancePolicy
    from camera_logs.node.resource_limits import ResourceLimits
    from camera_logs.node.write_pressure import write_latency_limit

    if not hasattr(worker, "performance_controller"):
        worker.performance_controller = PerformanceController()
    telemetry = worker.telemetry.value
    if worker.telemetry.age_seconds() is None or worker.telemetry.age_seconds() > 15:
        telemetry = {}
    status = worker.performance_controller.evaluate(
        PerformancePolicy.model_validate(config.get("performance") or {}), telemetry,
        write_latency | {"writeLatencyMs": write_latency.get("writeIoLatencyMs", write_latency.get("writeLatencyMs")),
                         "limitMs": write_latency_limit(config)},
        limits=ResourceLimits(**(telemetry.get("resourceLimits") or {
            "effectiveCpu": 1, "memoryLimitBytes": 0, "memoryAvailableBytes": 0, "source": "UNKNOWN"})),
        disk_percent=disk_percent)
    _effective = status["effective"]
    _pressured = status["state"] in {"THROTTLED", "RECOVERING"}
    buffering.configure(_effective)
    write_io.configure(_effective["writeConcurrency"])
    read_limiter.configure(_effective["readMiBPerSecond"] * 1024 * 1024)
    status.update(buffer=buffering.snapshot(), writeIo=write_io.snapshot(), archives=archive_snapshot())
    if "writeIoLatencyMs" in write_latency:
        status["writeIoLatency"] = {
            "p99Ms": write_latency.get("writeIoLatencyP99Ms", write_latency["writeIoLatencyMs"]),
            "pendingMs": write_latency.get("writeIoLatencyPendingMs", 0),
            "samples": write_latency.get("writeIoLatencySamples", 0),
            "windowSeconds": write_latency.get("writeIoLatencyWindowSeconds", 60),
        }
    status["admissionBlocked"] = status.get("admissionBlocked", False) or (
        status["buffer"]["reservedBytes"] >= status["buffer"]["limitBytes"] * .9
        and status["buffer"]["oldestPendingMs"] > write_latency_limit(config))
    worker.performance_status = status
    return status
