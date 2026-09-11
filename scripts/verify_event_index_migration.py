"""验证历史事件索引可平滑升级，并检查真实 Mongo 的实际查询计划。

脚本只操作随机临时数据库：先创建旧版本无名称索引，再连续调用两次仓储
初始化，最后对审计、运行和请求事件的正式分页排序形状运行 Explain。无论
成功或失败都会删除该数据库，不访问设备、任务或正式业务数据库。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from pymongo import AsyncMongoClient


def _has_stage(plan: object, stage: str) -> bool:
    """递归识别经典或 SBE Explain 计划中的指定阶段。"""
    if isinstance(plan, dict):
        query_plan = plan.get("queryPlan")
        return (plan.get("stage") == stage
                or (stage == "SORT" and "$sort" in plan)
                or (isinstance(query_plan, dict) and query_plan.get("stage") == stage)
                or any(_has_stage(value, stage) for value in plan.values()))
    return isinstance(plan, list) and any(_has_stage(value, stage) for value in plan)


def _execution_stats(plan: object) -> dict:
    """从聚合 $cursor 或普通 find Explain 中抽取执行统计。"""
    if isinstance(plan, dict):
        stats = plan.get("executionStats")
        if isinstance(stats, dict):
            return stats
        for value in plan.values():
            result = _execution_stats(value)
            if result:
                return result
    elif isinstance(plan, list):
        for value in plan:
            result = _execution_stats(value)
            if result:
                return result
    return {}


async def _explain(db, collection: str, query: dict, *, derived_time: bool = False) -> dict[str, object]:
    """返回不含数据正文、连接地址和完整计划树的查询计划摘要。"""
    if derived_time:
        command = {"aggregate": collection, "cursor": {}, "pipeline": [
            {"$match": query}, {"$addFields": {"_eventTime": {"$ifNull": ["$createdAt", "$detectedAt"]}}},
            {"$sort": {"_eventTime": -1, "_id": -1}}, {"$limit": 50},
        ]}
    else:
        command = {"find": collection, "filter": query, "sort": {"createdAt": -1, "_id": -1}, "limit": 50}
    explain = await db.command("explain", command, verbosity="executionStats")
    stats = _execution_stats(explain)
    plan = explain.get("queryPlanner", {}).get("winningPlan") or explain
    return {"ixscan": _has_stage(plan, "IXSCAN"), "collscan": _has_stage(plan, "COLLSCAN"),
            "sortStage": _has_stage(plan, "SORT"), "nReturned": stats.get("nReturned", 0),
            "keysExamined": stats.get("totalKeysExamined", 0), "docsExamined": stats.get("totalDocsExamined", 0)}


async def verify() -> dict[str, object]:
    """预装旧索引、双初始化、Explain 并确认临时库删除。"""
    configured = Settings()
    database_name = f"event_index_migration_verify_{uuid4().hex}"
    client = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    try:
        db = client[database_name]
        # 模拟已运行环境的旧匿名索引名称，覆盖此前 code 85 冲突路径。
        await db.request_events.create_index("createdAt", expireAfterSeconds=30 * 24 * 60 * 60)
        await db.request_events.create_index([("taskId", 1), ("createdAt", -1)])
        await db.request_events.create_index("requestId")
        repository = Repository(db, configured)
        await repository.initialize()
        await repository.initialize()

        stamp = datetime.now(UTC) - timedelta(days=1)
        await db.audit.insert_many([{"actor": f"user-{index % 4}", "action": "create_task",
                                     "targetId": f"task-{index % 8}", "createdAt": stamp + timedelta(seconds=index)}
                                    for index in range(1000)])
        await db.events.insert_many([{"nodeId": f"node-{index % 4}", "taskId": f"task-{index % 8}",
                                      "type": "CONNECTION_GAP", "createdAt": stamp + timedelta(seconds=index)}
                                     for index in range(1000)])
        await db.request_events.insert_many([{"taskId": f"task-{index % 8}", "route": "/api/v1/tasks/{id}",
                                              "httpStatus": 200, "createdAt": stamp + timedelta(seconds=index)}
                                             for index in range(1000)])
        lower, upper = stamp, stamp + timedelta(days=1)
        plans = {
            "audit_default": await _explain(db, "audit", {}),
            "audit": await _explain(db, "audit", {"actor": "user-1", "createdAt": {"$gte": lower, "$lt": upper}}),
            "runtime": await _explain(db, "events", {"nodeId": "node-1", "createdAt": {"$gte": lower, "$lt": upper}},
                                      derived_time=True),
            "request": await _explain(db, "request_events", {"taskId": "task-1", "createdAt": {"$gte": lower, "$lt": upper}}),
            "request_default": await _explain(db, "request_events", {}),
        }
        if any(not result["ixscan"] or result["collscan"] for result in plans.values()):
            raise AssertionError(f"事件查询未命中预期索引: {plans}")
        if any(plans[name]["sortStage"] for name in ("audit", "audit_default", "request", "request_default")):
            raise AssertionError(f"普通事件查询仍存在未由复合索引满足的排序: {plans}")
        return {"passed": True, "legacyIndexesReused": True, "initializedTwice": True, "plans": plans}
    finally:
        await client.drop_database(database_name)
        deleted = database_name not in await client.list_database_names()
        await client.close()
        if not deleted:
            raise AssertionError("临时事件索引验证数据库未删除")


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False, sort_keys=True))
