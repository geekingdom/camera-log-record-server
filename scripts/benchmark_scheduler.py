"""在随机临时 MongoDB 中测量一次纯元数据调度周期，不启动采集连接。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.tasks.scheduler import schedule_once
from pymongo import AsyncMongoClient, monitoring


class CommandMetrics(monitoring.CommandListener):
    """只记录调度周期内的命令类型与耗时，不保存命令正文或连接信息。"""

    def __init__(self) -> None:
        self.starts: dict[int, tuple[str, float]] = {}
        self.counts: Counter[str] = Counter()
        self.durations_ms: Counter[str] = Counter()

    def reset(self) -> None:
        """清空建库与种子写入指标，只保留随后调度周期的观测。"""
        self.starts.clear()
        self.counts.clear()
        self.durations_ms.clear()

    def started(self, event) -> None:
        """按驱动请求编号记录开始时刻，避免保存可能含敏感内容的命令体。"""
        self.starts[event.request_id] = (event.command_name, time.perf_counter())

    def succeeded(self, event) -> None:
        """累计已完成命令的数量和往返耗时。"""
        started = self.starts.pop(event.request_id, None)
        if started is None:
            return
        command, instant = started
        self.counts[command] += 1
        self.durations_ms[command] += (time.perf_counter() - instant) * 1000

    def failed(self, event) -> None:
        """移除失败命令的计时状态，实际异常由调度调用向上传递。"""
        self.starts.pop(event.request_id, None)


async def seed(repo: Repository) -> None:
    """建立八个容量为 100 的虚拟节点和 500 个待分配任务，完全不包含设备凭据。"""
    await repo.db.nodes.insert_many([
        {"id": f"benchmark-node-{number}", "heartbeat": now(), "diskPercent": 10,
         "accepting": True, "capacity": 100, "writeLatencyMs": 0, "inputBytesPerSecond": 0}
        for number in range(8)
    ])
    await repo.db.tasks.insert_many([
        {"id": f"benchmark-task-{number}", "ip": f"198.51.100.{number // 100 + 1}",
         "port": 2000 + number, "nodeId": None, "status": "PENDING", "desiredState": "RUNNING",
         "generation": 0}
        for number in range(500)
    ])


async def benchmark() -> dict[str, object]:
    """运行一次调度并返回可比较的安全摘要，finally 永远删除随机临时数据库。"""
    settings = Settings()
    database_name = f"scheduler_benchmark_{uuid4().hex}"
    if database_name == settings.database_name:
        raise RuntimeError("调度基线临时数据库名不能等于主库名")
    metrics = CommandMetrics()
    client = AsyncMongoClient(settings.mongo_uri, event_listeners=[metrics], serverSelectionTimeoutMS=5000,
                              tz_aware=True, w="majority", journal=True)
    try:
        repo = Repository(client[database_name], settings.model_copy(update={"cluster_capacity": 500}))
        await repo.initialize()
        await seed(repo)
        metrics.reset()
        started = time.perf_counter()
        await schedule_once(repo)
        elapsed_ms = (time.perf_counter() - started) * 1000
        # 调度结束立刻冻结监听器，后续分配与容量断言的 count_documents 不计入基线。
        command_counts = dict(metrics.counts)
        command_durations = dict(metrics.durations_ms)
        assignments = await repo.db.tasks.count_documents({"nodeId": {"$ne": None}})
        per_node = {
            f"benchmark-node-{number}": await repo.db.tasks.count_documents({"nodeId": f"benchmark-node-{number}"})
            for number in range(8)
        }
        if assignments != 500:
            raise AssertionError(f"调度分配数量异常：{assignments}")
        if max(per_node.values(), default=0) > 100:
            raise AssertionError("单节点调度数量超过容量 100")
        return {
            "passed": True,
            "nodes": 8,
            "pendingTasks": 500,
            "assignedTasks": assignments,
            "perNodeMax": max(per_node.values(), default=0),
            "elapsedMs": round(elapsed_ms, 3),
            "commands": {name: {"count": command_counts[name], "durationMs": round(command_durations[name], 3)}
                         for name in sorted(command_counts)},
        }
    finally:
        try:
            await client.drop_database(database_name)
        finally:
            await client.close()


def parse_args() -> argparse.Namespace:
    """可选保存一次基线；拒绝覆盖现存结果以保留 before/after 证据。"""
    parser = argparse.ArgumentParser(description="本机真实 MongoDB 调度基线")
    parser.add_argument("--output", type=Path, help="可选 JSON 输出路径，路径已存在时拒绝覆盖")
    return parser.parse_args()


def main() -> None:
    """运行异步基线并仅输出安全汇总。"""
    args = parse_args()
    if args.output and args.output.exists():
        raise SystemExit(f"拒绝覆盖已有基线文件：{args.output}")
    result = asyncio.run(benchmark())
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
