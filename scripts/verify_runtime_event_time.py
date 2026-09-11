"""在真实 Mongo 随机库验证运行事件时间回填、兼容分页和索引查询计划。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from camera_logs.administration.event_queries import _derived_fields, runtime_event_page
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.common.runtime_event_time import backfill_runtime_event_created_at
from pymongo import AsyncMongoClient


def check(condition: bool, message: str) -> None:
    """用不含事件正文的中文断言说明真实数据库验证失败原因。"""
    if not condition:
        raise AssertionError(message)


def has_stage(value: object, stage: str) -> bool:
    """递归识别经典/SBE Explain 中的执行阶段。"""
    if isinstance(value, dict):
        plan = value.get("queryPlan")
        return (
            value.get("stage") == stage
            or (isinstance(plan, dict) and plan.get("stage") == stage)
            or any(has_stage(item, stage) for item in value.values())
        )
    return isinstance(value, list) and any(has_stage(item, stage) for item in value)


def execution_stats(value: object) -> dict:
    """从 find 或 aggregate Explain 结果定位 executionStats。"""
    if isinstance(value, dict):
        stats = value.get("executionStats")
        if isinstance(stats, dict):
            return stats
        for item in value.values():
            found = execution_stats(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = execution_stats(item)
            if found:
                return found
    return {}


def explain_summary(value: dict) -> dict[str, int | bool]:
    """输出无业务正文的计划摘要，便于 CI 保留失败证据。"""
    stats = execution_stats(value)
    plan = value.get("queryPlanner", {}).get("winningPlan", value)
    return {
        "ixscan": has_stage(plan, "IXSCAN"),
        "sort": has_stage(value.get("stages", plan), "SORT"),
        "returned": int(stats.get("nReturned", 0)),
        "keysExamined": int(stats.get("totalKeysExamined", 0)),
        "docsExamined": int(stats.get("totalDocsExamined", 0)),
    }


async def explain_find(db, query: dict) -> dict[str, int | bool]:
    """以运行事件正式稳定排序执行有限 find Explain。"""
    value = await db.command(
        "explain",
        {
            "find": "events",
            "filter": query,
            "sort": {"createdAt": -1, "_id": -1},
            "limit": 50,
        },
        verbosity="executionStats",
    )
    return explain_summary(value)


async def explain_probe(db) -> dict[str, int | bool]:
    """检查没有旧时间记录时，兼容性探测不会扫描运行事件全集。"""
    value = await db.command(
        "explain",
        {
            "find": "events",
            "filter": {"createdAt": None, "detectedAt": {"$ne": None}},
            "limit": 1,
        },
        verbosity="executionStats",
    )
    return explain_summary(value)


async def explain_derived(db, query: dict) -> dict[str, int | bool]:
    """验证派生 outcome 在索引排序后过滤，不要求结果筛选索引。"""
    outcomes, levels = _derived_fields()
    value = await db.command(
        "explain",
        {
            "aggregate": "events",
            "cursor": {},
            "pipeline": [
                {"$match": query},
                {"$sort": {"createdAt": -1, "_id": -1}},
                {"$addFields": outcomes},
                {"$addFields": levels},
                {"$match": {"_derivedOutcome": "UNKNOWN"}},
                {"$limit": 50},
            ],
        },
        verbosity="executionStats",
    )
    return explain_summary(value)


def ids(page: dict) -> list[str]:
    """提取排序后的公共 ID，避免比较展示器附加的名称字段。"""
    return [item["id"] for item in page["items"]]


async def verify() -> dict[str, object]:
    """创建随机库验证迁移前后语义和真实索引计划，finally 始终删除临时库。"""
    configured = Settings()
    database_name = "runtime_event_time_verify_" + uuid4().hex
    client = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    try:
        hello = await client.admin.command("hello")
        if not hello.get("setName"):
            raise RuntimeError("运行事件时间验证需要 MongoDB 副本集")
        repo = Repository(client[database_name], configured)
        await repo.initialize()
        db = repo.db
        start = datetime(2026, 9, 11, 8, tzinfo=UTC)
        window = {"$gte": start, "$lt": start + timedelta(minutes=4)}
        legacy = [
            {
                "_id": "legacy-missing",
                "id": "legacy-missing",
                "taskId": "legacy",
                "type": "CONNECTION_GAP",
                "detectedAt": start + timedelta(minutes=1),
            },
            {
                "_id": "legacy-null",
                "id": "legacy-null",
                "taskId": "legacy",
                "type": "CONNECTION_GAP",
                "createdAt": None,
                "detectedAt": start + timedelta(minutes=2),
            },
            {
                "_id": "created-wins",
                "id": "created-wins",
                "taskId": "legacy",
                "type": "CONNECTION_GAP",
                "createdAt": start + timedelta(minutes=3),
                "detectedAt": start + timedelta(minutes=9),
            },
            {
                "_id": "legacy-array",
                "id": "legacy-array",
                "taskId": "array",
                "type": "CONNECTION_GAP",
                "createdAt": None,
                "detectedAt": [start + timedelta(minutes=2)],
            },
        ]
        await db.events.insert_many(legacy)
        before = await runtime_event_page(db, {"taskId": "legacy"}, 1, 20)
        before_ids = ids(before)
        check(
            before_ids == ["created-wins", "legacy-null", "legacy-missing"],
            "迁移前未优先 createdAt 并回退 detectedAt 排序",
        )
        before_range = await runtime_event_page(db, {"taskId": "legacy"}, 1, 20, time_range=window)
        check(ids(before_range) == before_ids, "迁移前时间范围未覆盖缺失/null createdAt 历史")

        migrated = await backfill_runtime_event_created_at(db, apply=True)
        check(migrated["updated"] == 2, "迁移没有精确补齐缺失/null createdAt 历史")
        stored = {row["id"]: row async for row in db.events.find({"taskId": {"$in": ["legacy", "array"]}})}
        check(
            stored["legacy-missing"]["createdAt"] == stored["legacy-missing"]["detectedAt"],
            "缺失时间未回填 detectedAt",
        )
        check(
            stored["legacy-null"]["createdAt"] == stored["legacy-null"]["detectedAt"],
            "null 时间未回填 detectedAt",
        )
        check(
            stored["created-wins"]["createdAt"] == start + timedelta(minutes=3), "已有 createdAt 被错误覆盖"
        )
        check(stored["legacy-array"]["createdAt"] is None, "日期数组被错误回填为标量时间")
        after = await runtime_event_page(db, {"taskId": "legacy"}, 1, 20)
        after_range = await runtime_event_page(db, {"taskId": "legacy"}, 1, 20, time_range=window)
        check(
            ids(after) == before_ids and ids(after_range) == before_ids, "迁移后列表或时间范围与迁移前不等价"
        )

        # 无法安全迁移的数组记录保留在前半段验证；删除仅限本随机库，随后证明全部
        # 可排序事件已规范化时接口走普通索引页而不是兼容聚合。
        await db.events.delete_one({"_id": "legacy-array"})
        current = []
        for index in range(20_000):
            current.append(
                {
                    "_id": f"current-{index:05d}",
                    "id": f"current-{index:05d}",
                    "taskId": "hot-task",
                    "nodeId": "hot-node",
                    "type": "CONNECTION_GAP",
                    "createdAt": start + timedelta(hours=1, seconds=index),
                }
            )
            if len(current) == 1000:
                await db.events.insert_many(current)
                current = []
        if current:
            await db.events.insert_many(current)
        default = await explain_find(db, {})
        filtered = await explain_find(db, {"taskId": "hot-task"})
        node = await explain_find(db, {"nodeId": "hot-node"})
        event_type = await explain_find(db, {"type": "CONNECTION_GAP"})
        derived = await explain_derived(db, {"taskId": "hot-task"})
        derived_page = await runtime_event_page(db, {"taskId": "hot-task", "outcome": "UNKNOWN"}, 1, 50)
        probe = await explain_probe(db)
        for name, summary in {"default": default, "task": filtered, "node": node, "type": event_type}.items():
            check(summary["ixscan"] and not summary["sort"], f"{name} 查询未使用无排序索引计划")
            check(
                summary["returned"] == 50 and summary["keysExamined"] == 50 and summary["docsExamined"] == 50,
                f"{name} 查询未达到50返回/50扫描",
            )
        check(
            derived["ixscan"] and not derived["sort"],
            "派生 outcome 查询未在索引排序后完成过滤",
        )
        check(len(derived_page["items"]) == 50, "派生 outcome 正式分页未返回50条")
        check(
            probe["returned"] == 0 and probe["keysExamined"] <= 1 and probe["docsExamined"] <= 1,
            "无旧时间记录时兼容性探测扫描量不受默认时间索引约束",
        )
        return {
            "passed": True,
            "temporaryDatabase": database_name,
            "migrated": migrated,
            "plans": {
                "default": default,
                "task": filtered,
                "node": node,
                "type": event_type,
                "derivedOutcome": derived,
                "legacyProbe": probe,
            },
        }
    finally:
        await client.drop_database(database_name)
        removed = database_name not in await client.list_database_names()
        await client.close()
        if not removed:
            raise AssertionError("随机运行事件验证数据库未被删除")


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False, sort_keys=True))
