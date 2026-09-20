"""验证跨机器墙上时钟偏移时，节点心跳和遥测仍按平台接收时间工作。

本脚本只连接回环 MongoDB 的随机数据库，并创建临时日志根目录；不启动 HTTP
服务、不访问设备。两次 Worker.tick 分别模拟慢八小时和快八小时的节点墙上时间，
心跳必须由 MongoDB 的 ``$currentDate`` 连续生成。随后验证高 CPU 只拒绝新调度、
恢复后领取可用，以及停止上报后的心跳会在平台时钟下离线。
"""

import asyncio
import ipaddress
import json
import time
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.node.health import measurements, node_health, rank_nodes
from camera_logs.node.worker import Worker
from camera_logs.tasks.claim import claim_task
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.uri_parser import parse_uri


def check(condition: bool, message: str) -> None:
    """让失败信息直接对应本次跨时钟验收的不变量。"""
    if not condition:
        raise AssertionError(message)


def local_mongo_uri(uri: str) -> bool:
    """仅允许所有 Mongo 成员为回环地址，避免随机库误连远端环境。"""
    if uri.startswith("mongodb+srv://"):
        return False
    try:
        nodes = parse_uri(uri)["nodelist"]
        return bool(nodes) and all(
            host == "localhost" or ipaddress.ip_address(host).is_loopback
            for host, _port in nodes
        )
    except Exception:  # noqa: BLE001 - 无法严格证明本机连接时拒绝执行。
        return False


class StableTelemetry:
    """为 Worker.tick 提供单调年龄稳定的遥测，避免真实机器负载影响验证。"""

    def __init__(self, sample: dict[str, object]) -> None:
        self.value = sample
        self.sample_started = time.monotonic() - 0.2

    def start_if_idle(self) -> None:
        """验证仅检查心跳写入，不额外启动采样线程。"""

    def age_seconds(self) -> float:
        """模拟 TelemetryRuntime 的单调年龄，不能依赖节点墙上时间。"""
        return time.monotonic() - self.sample_started

    async def close(self) -> None:
        """保持与 TelemetryRuntime 相同的关闭接口。"""


def telemetry(cpu_percent: float) -> dict[str, object]:
    """构造完整连续采样；sampledAt 故意保留节点本机时间供校准检查。"""
    return {
        "sampledAt": now(),
        "scope": "HOST",
        "status": "OK",
        "cpuPercent": cpu_percent,
        "memoryPercent": 12.0,
        # 本脚本隔离墙上时钟；新性能准入仍需要明确的可用资源，不能用缺失值冒充空闲。
        "resourceLimits": {"effectiveCpu": 4, "memoryLimitBytes": 2 * 1024**3,
                           "memoryAvailableBytes": 1024**3, "source": "TEST"},
        "networkUploadBytesPerSecond": 100.0,
        "networkDownloadBytesPerSecond": 200.0,
    }


async def tick_with_clock(worker: Worker, offset: timedelta) -> dict[str, object]:
    """在给定节点墙上偏移下执行一次正式 tick，并读取 Mongo 实际写入。"""
    clock = now() + offset
    disk = type("Disk", (), {"used": 10, "total": 100, "free": 90})()
    # telemetry 与 workerReportedAt 都来自同一节点墙上时钟，必须一起偏移。
    worker.telemetry.value["sampledAt"] = clock
    with patch("camera_logs.node.worker.now", return_value=clock), \
            patch("camera_logs.node.health.now", return_value=clock), \
            patch("camera_logs.node.worker.shutil.disk_usage", return_value=disk):
        await worker.tick()
    node = await worker.repo.db.nodes.find_one({"id": worker.repo.settings.node_id})
    check(node is not None, "Worker.tick 未写入节点心跳")
    return node


async def verify() -> dict[str, object]:
    """在真实 Mongo 中完成时钟、遥测校准和事务领取三组验收。"""
    configured = Settings()
    check(local_mongo_uri(configured.mongo_uri), "本验证仅允许本机回环 MongoDB")
    database_name = f"node_heartbeat_clock_verify_{uuid4().hex}"
    check(database_name != configured.database_name, "验证脚本不能使用配置的主数据库")
    client = AsyncMongoClient(
        configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True,
    )
    failure: BaseException | None = None
    try:
        # 目录由 TemporaryDirectory 统一回收，Mongo finally 只删除本次随机库。
        with TemporaryDirectory(prefix="camera-node-heartbeat-clock-") as temporary:
            settings = Settings(
                _env_file=None,
                mongo_uri=configured.mongo_uri,
                database_name=database_name,
                encryption_key=Fernet.generate_key().decode(),
                log_root=Path(temporary) / "logs",
                node_id="clock-skew-node",
                node_url="http://127.0.0.1:18081",
                start_background=False,
            )
            repo = Repository(client[database_name], settings)
            await repo.initialize()
            worker = Worker(repo)
            # 避开低频维护与空 NFS 扫描，验证范围只包含本次心跳写入。
            worker.last_maintenance = worker.last_coredump_scan = time.monotonic()
            worker.telemetry = StableTelemetry(telemetry(10.0))

            first = await tick_with_clock(worker, timedelta(hours=-8))
            await asyncio.sleep(0.02)
            second = await tick_with_clock(worker, timedelta(hours=8))
            first_heartbeat, second_heartbeat = first["heartbeat"], second["heartbeat"]
            check(second_heartbeat > first_heartbeat, "MongoDB 平台心跳未在连续 tick 中递增")
            check(first["workerReportedAt"] < first_heartbeat - timedelta(hours=7), "慢八小时节点时间未仅作为诊断字段保存")
            check(second["workerReportedAt"] > second_heartbeat + timedelta(hours=7), "快八小时节点时间未仅作为诊断字段保存")
            check(first["telemetry"]["sampledAt"] < first_heartbeat - timedelta(hours=7), "慢八小时遥测未模拟节点墙上时间")
            check(second["telemetry"]["sampledAt"] > second_heartbeat + timedelta(hours=7), "快八小时遥测未模拟节点墙上时间")
            check(node_health(second, second_heartbeat)["status"] != "OFFLINE", "连续上报却因节点时钟偏移被判离线")
            calibrated = measurements(second, second_heartbeat)
            check(calibrated.get("workerSampledAt") == second["telemetry"]["sampledAt"], "遥测未保留节点上报时间用于诊断")
            check(abs((second_heartbeat - calibrated["sampledAt"]).total_seconds() - second["telemetryAgeSeconds"]) < 0.1,
                  "遥测采样时间未按 Mongo 心跳和单调年龄校准")

            # 高 CPU 的采样墙上时间与节点时钟无关，仍必须拒绝排名和真实事务领取。
            worker.telemetry = StableTelemetry(telemetry(96.0))
            busy = await tick_with_clock(worker, timedelta(hours=8))
            check(rank_nodes([busy], {}) == [], "高 CPU 节点仍进入调度候选")
            task = {
                "id": "clock-claim", "ip": "127.0.0.1", "port": 2200, "nodeId": None,
                "status": "PENDING", "desiredState": "RUNNING", "generation": 0,
            }
            await repo.db.tasks.insert_one(task)
            check(await claim_task(repo, task, settings.node_id) is None, "高 CPU 节点仍能领取新任务")
            check(await repo.db.runs.count_documents({}) == 0, "拒绝高 CPU 领取后留下运行记录")
            check(await repo.db.endpoint_locks.count_documents({}) == 0, "拒绝高 CPU 领取后留下运行锁")

            # 压力恢复后同一任务应能通过真实副本集事务领取，证明准入不是永久阻塞。
            worker.telemetry = StableTelemetry(telemetry(10.0))
            recovered = await tick_with_clock(worker, timedelta(hours=-8))
            check(rank_nodes([recovered], {})[0][1] == settings.node_id, "CPU 恢复后节点未回到调度候选")
            claimed = await claim_task(repo, task, settings.node_id)
            check(claimed is not None, "CPU 恢复后真实 Mongo 领取不可用")
            check(await repo.db.runs.count_documents({"id": claimed["runId"]}) == 1, "领取未创建唯一运行记录")

            # 不再调用 tick 即代表停止上报；用平台评估时间跨越 30 秒边界验证离线。
            offline = node_health(recovered, recovered["heartbeat"] + timedelta(seconds=31))
            check(offline["status"] == "OFFLINE", "停止上报后的旧 Mongo 心跳未转为离线")
            return {
                "passed": True,
                "databaseHeartbeatContinuous": True,
                "workerClockOffsetsHours": [-8, 8],
                "telemetryCalibratedByHeartbeat": True,
                "highCpuRejectedScheduling": True,
                "claimRecoveredAfterCpuNormal": True,
                "staleHeartbeatOffline": True,
                "temporaryDatabaseDropped": True,
                "temporaryLogRootDropped": True,
            }
    except BaseException as error:
        # 连接、断言和清理失败中只报告第一个，不能让不可达 Mongo 覆盖验收根因。
        failure = error
        raise
    finally:
        try:
            await client.drop_database(database_name)
        except Exception:  # 原失败优先，连接不可达时没有可清理的数据。
            if failure is None:
                raise
        finally:
            await client.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False))
