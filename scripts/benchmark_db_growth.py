"""测量长期增长集合的 Mongo 查询计划与分页成本。

脚本只在随机临时数据库中写入不含凭据和设备正文的元数据样本，执行与正式
接口相同的过滤、排序和分页查询，并通过 ``explain("executionStats")`` 输出
扫描键、扫描文档和命中计划。MongoMock 不具备真实查询计划，不能作为索引
命中证据；没有可用的真实 Mongo 时脚本会明确失败而不会伪造结果。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from pymongo import AsyncMongoClient
from pymongo.errors import OperationFailure

COLLECTIONS = ("authentication_records", "audit", "events", "request_events")

# 仅在随机基准库中创建的候选索引。生产库是否采用，需结合实际 Explain、
# 写入成本和索引占用评审；脚本不会替线上数据库修改索引。
CANDIDATE_INDEXES = {
    # MongoDB 不允许把 _id 放入复合索引；稳定 _id 仅作为同一时间戳的
    # 最后排序键，Explain 仍能测量过滤和时间排序的主要成本。
    "audit": [("actor", 1), ("action", 1), ("createdAt", -1)],
    "events": [("nodeId", 1), ("taskId", 1), ("createdAt", -1)],
    "request_events": [("taskId", 1), ("createdAt", -1)],
}


def _contains_stage(plan: object, stage: str) -> bool:
    """递归判断 explain 计划是否包含指定阶段，兼容经典和 SBE 计划树。"""
    if isinstance(plan, dict):
        query_plan = plan.get("queryPlan")
        if plan.get("stage") == stage or (isinstance(query_plan, dict) and query_plan.get("stage") == stage):
            return True
        return any(_contains_stage(value, stage) for value in plan.values())
    if isinstance(plan, list):
        return any(_contains_stage(value, stage) for value in plan)
    return False


def summarize_explain(explain: dict) -> dict[str, object]:
    """提取安全的 Explain 摘要，不返回连接地址、查询正文或样本数据。"""
    execution = explain.get("executionStats") or {}
    winning = (explain.get("queryPlanner") or {}).get("winningPlan")
    keys = int(execution.get("totalKeysExamined", 0))
    docs = int(execution.get("totalDocsExamined", 0))
    returned = int(execution.get("nReturned", 0))
    return {
        "plan": "IXSCAN" if _contains_stage(winning, "IXSCAN") else (
            "COLLSCAN" if _contains_stage(winning, "COLLSCAN") else "OTHER"),
        "nReturned": returned,
        "totalKeysExamined": keys,
        "totalDocsExamined": docs,
        "executionTimeMillis": int(execution.get("executionTimeMillis", 0)),
        "selective": docs <= max(returned * 20, 100),
    }


def query_cases() -> dict[str, dict[str, object]]:
    """返回与生产接口一致的增长集合查询及稳定排序定义。"""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=31)
    return {
        "authentication_recent": {
            "collection": "authentication_records",
            "filter": {"resourceId": "resource-0001", "result": "SUCCESS", "identityChanged": False,
                        "createdAt": {"$gte": start, "$lt": end}},
            "sort": {"createdAt": -1, "id": -1}, "skip": 0, "limit": 20,
        },
        "authentication_deep_page": {
            "collection": "authentication_records",
            "filter": {"resourceId": "resource-0001", "createdAt": {"$gte": start, "$lt": end}},
            "sort": {"createdAt": -1, "id": -1}, "skip": 5000, "limit": 20,
        },
        "audit_filtered": {
            "collection": "audit",
            "filter": {"actor": "user-0001", "action": "resource_health_stop:OFFLINE",
                        "createdAt": {"$gte": start, "$lt": end}},
            "sort": {"createdAt": -1, "_id": -1}, "skip": 0, "limit": 50,
        },
        "runtime_filtered": {
            "collection": "events",
            "filter": {"nodeId": "collector-01", "taskId": "task-0001",
                        "createdAt": {"$gte": start, "$lt": end}},
            "sort": {"createdAt": -1, "_id": -1}, "skip": 0, "limit": 50,
        },
        "request_filtered": {
            "collection": "request_events",
            "filter": {"taskId": "task-0001", "createdAt": {"$gte": start, "$lt": end}},
            "sort": {"createdAt": -1, "_id": -1}, "skip": 0, "limit": 50,
        },
    }


async def _seed(db, rows: int) -> None:
    """批量写入固定大小元数据，避免一次构造超大 Python 列表。"""
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    for offset in range(0, rows, 1000):
        batch = []
        for index in range(offset, min(offset + 1000, rows)):
            # 低基数字段让默认 20,000 行样本既覆盖热门资源的深页，又不会把
            # 单条查询误测成几乎无结果的点查。
            resource = f"resource-{index % 2:04d}"
            task = f"task-{index % 8 + 1:04d}"
            base = {"createdAt": stamp + timedelta(seconds=index), "id": f"row-{index:08d}"}
            batch.extend([
                {**base, "resourceId": resource, "result": "SUCCESS" if index % 3 else "OFFLINE",
                 "identityChanged": index % 5 == 0},
                {**base, "actor": f"user-{index % 10:04d}",
                 "action": "resource_health_stop:OFFLINE" if index % 4 else "create_task"},
                {**base, "nodeId": f"collector-{index % 8 + 1:02d}", "taskId": task,
                 "type": "CONNECTION_GAP"},
                {**base, "taskId": task, "method": "GET", "route": "/api/v1/tasks/{id}"},
            ])
        await db.authentication_records.insert_many(batch[0::4])
        await db.audit.insert_many(batch[1::4])
        await db.events.insert_many(batch[2::4])
        await db.request_events.insert_many(batch[3::4])


async def _explain_queries(repo: Repository) -> dict[str, dict[str, object]]:
    """执行所有声明的有限查询并提取 executionStats。"""
    results = {}
    for name, case in query_cases().items():
        command = {"find": case["collection"], "filter": case["filter"], "sort": case["sort"],
                   "skip": case["skip"], "limit": case["limit"]}
        explain = await repo.db.command("explain", command, verbosity="executionStats")
        results[name] = summarize_explain(explain)
    return results


async def _create_candidate_indexes(repo: Repository) -> None:
    """在临时库安装候选索引，供 before/after 对照；不触碰正式数据库。"""
    for collection, keys in CANDIDATE_INDEXES.items():
        try:
            await repo.db[collection].create_index(keys, name=f"benchmark_{collection}_filter_time")
        except OperationFailure as error:
            # Repository.initialize 已有同键索引时，Mongo 只拒绝改名；该索引
            # 已足以完成 before/after 对照，记录为复用而继续基准。
            if error.code != 85:
                raise


async def benchmark(rows: int, *, compare_indexes: bool = False) -> dict[str, object]:
    """运行 Explain 基准并始终删除随机数据库。"""
    settings = Settings()
    database_name = f"db_growth_benchmark_{uuid4().hex}"
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    started = time.perf_counter()
    try:
        repo = Repository(client[database_name], settings)
        await repo.initialize()
        await _seed(repo.db, rows)
        before = await _explain_queries(repo)
        result = {"passed": True, "rowsPerCollection": rows,
                  "elapsedMs": round((time.perf_counter() - started) * 1000, 3), "queries": before}
        if compare_indexes:
            await _create_candidate_indexes(repo)
            result["candidateQueries"] = await _explain_queries(repo)
        return result
    finally:
        await client.drop_database(database_name)
        await client.close()


def parse_args() -> argparse.Namespace:
    """解析安全的样本规模和可选摘要输出路径。"""
    parser = argparse.ArgumentParser(description="真实 Mongo 增长集合 Explain 基准")
    parser.add_argument("--rows", type=int, default=20_000, choices=range(1_000, 1_000_001), metavar="1000..1000000")
    parser.add_argument("--compare-indexes", action="store_true",
                        help="在临时库创建候选增长集合索引并输出 before/after Explain")
    parser.add_argument("--output", type=Path, help="可选 JSON 输出路径；已存在时拒绝覆盖")
    return parser.parse_args()


def main() -> None:
    """运行基准；连接失败直接返回非零状态，避免输出假成功。"""
    args = parse_args()
    if args.output and args.output.exists():
        raise SystemExit(f"拒绝覆盖已有基线文件：{args.output}")
    result = asyncio.run(benchmark(args.rows, compare_indexes=args.compare_indexes))
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
