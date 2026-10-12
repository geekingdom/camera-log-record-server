"""把节点性能决策应用到采集、写入和后台读取，管理员阈值保持独立。"""

import time

from camera_logs.collection import buffering

_effective = {"readMiBPerSecond": 50}
_pressured = False
_archives = {}
_disk_archives = {"pendingJobs": 0, "oldestSeconds": 0, "sampledAt": 0}


def background_settings():
    """压缩作业实际执行时读取后台预算，不在原子发布中途强行取消。"""
    return _effective["readMiBPerSecond"] * 1024 * 1024, not _pressured


def archive_started(token, *, persisted: bool = False):
    """只保留活动归档的开始时刻，完成后删除，不增加数据库历史集合。"""
    _archives[token] = {"queuedAt": time.monotonic(), "runningAt": None, "persisted": persisted}


def archive_running(token):
    """只有获得压缩执行槽后才计入处理中，避免把进程队列当作磁盘耗时。"""
    if token in _archives:
        _archives[token]["runningAt"] = time.monotonic()


def archive_finished(token):
    """无论成功失败均结束当前等待计时，失败仍由既有归档恢复流程处理。"""
    _archives.pop(token, None)


def archive_spool_snapshot(pending_jobs: int, oldest_seconds: float) -> None:
    """维护扫描更新磁盘 spool 快照，心跳直接复用以免重复递归日志目录。"""
    global _disk_archives
    _disk_archives = {"pendingJobs": max(0, pending_jobs), "oldestSeconds": max(0, oldest_seconds),
                      "sampledAt": time.monotonic()}


def archive_snapshot():
    """分别报告排队和执行时间；总耗时保留以兼容旧节点看板的告警合同。"""
    tick = time.monotonic()
    running = [item["runningAt"] for item in _archives.values() if item["runningAt"] is not None]
    persisted = sum(1 for item in _archives.values() if item["persisted"])
    persisted_running = sum(1 for item in _archives.values() if item["persisted"] and item["runningAt"] is not None)
    transient = len(_archives) - persisted
    transient_waiting = sum(1 for item in _archives.values() if not item["persisted"] and item["runningAt"] is None)

    def oldest(values):
        return max(0, tick - min(values)) if values else 0

    disk_pending = max(_disk_archives["pendingJobs"], persisted)
    disk_waiting = max(0, disk_pending - persisted_running)
    # 没有磁盘积压时不能把进程单调时钟的年龄冒充归档等待，引发空闲节点告警。
    disk_oldest = (_disk_archives["oldestSeconds"] + max(0, tick - _disk_archives["sampledAt"])) \
        if _disk_archives["pendingJobs"] else 0
    return {"pendingJobs": disk_pending + transient,
            "waitingJobs": disk_waiting + transient_waiting, "runningJobs": len(running),
            "diskPendingJobs": _disk_archives["pendingJobs"],
            "oldestSeconds": max(oldest([item["queuedAt"] for item in _archives.values()]), disk_oldest),
            "oldestWaitingSeconds": max(oldest([item["queuedAt"] for item in _archives.values()
                                                  if not item["persisted"] and item["runningAt"] is None]),
                                       disk_oldest if disk_waiting else 0),
            "oldestRunningSeconds": oldest(running)}


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
