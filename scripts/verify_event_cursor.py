"""在真实 Mongo 随机库验证管理事件游标分页，不读取设备或真实日志。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from bson import ObjectId
from camera_logs.administration.event_queries import event_cursor_page, runtime_event_cursor_page
from camera_logs.common.config import Settings
from camera_logs.common.event_indexes import install_event_indexes
from fastapi import HTTPException
from pymongo import AsyncMongoClient


def check(condition: bool, message: str) -> None:
    """以不含游标或事件正文的中文断言中断不可信的验证结果。"""
    if not condition:
        raise AssertionError(message)


def has_stage(value: object, stage: str) -> bool:
    """递归识别经典或 SBE Explain 中的执行阶段。"""
    if isinstance(value, dict):
        plan = value.get("queryPlan")
        return (value.get("stage") == stage or (isinstance(plan, dict) and plan.get("stage") == stage)
                or any(has_stage(item, stage) for item in value.values()))
    return isinstance(value, list) and any(has_stage(item, stage) for item in value)


def execution_stats(value: object) -> dict:
    """从 find Explain 结果中提取执行统计，兼容不同 Mongo 执行引擎。"""
    if isinstance(value, dict):
        if isinstance(value.get("executionStats"), dict):
            return value["executionStats"]
        for item in value.values():
            if found := execution_stats(item):
                return found
    if isinstance(value, list):
        for item in value:
            if found := execution_stats(item):
                return found
    return {}


def plan_shape(value: object) -> object:
    """提取 Explain 的紧凑阶段树，失败信息不携带游标、事件正文或业务字段。"""
    if not isinstance(value, dict):
        return value
    shape = {key: value[key] for key in ("stage", "planNodeId") if key in value}
    for key in ("inputStage", "inputStages", "queryPlan"):
        if key in value:
            child = value[key]
            shape[key] = [plan_shape(item) for item in child] if isinstance(child, list) else plan_shape(child)
    return shape


class CapturingCursor:
    """转发真实游标并记录链式排序与页大小，供 Explain 使用实际游标条件。"""

    def __init__(self, cursor, capture: dict) -> None:
        self.cursor, self.capture = cursor, capture

    def sort(self, value):
        self.capture["sort"] = value
        self.cursor = self.cursor.sort(value)
        return self

    def limit(self, value):
        self.capture["limit"] = value
        self.cursor = self.cursor.limit(value)
        return self

    def skip(self, value):
        self.capture["skip"] = value
        self.cursor = self.cursor.skip(value)
        return self

    def __aiter__(self):
        return self.cursor.__aiter__()


class CapturingCollection:
    """只拦截目标事件集合的查询描述，其他数据库操作直接交给真实集合。"""

    def __init__(self, collection, capture: dict, owner) -> None:
        self.collection, self.capture, self.owner = collection, capture, owner

    def find(self, *args, **kwargs):
        self.owner.query_count += 1
        self.capture.clear()
        self.capture.update(kind="find", filter=args[0] if args else kwargs.get("filter", {}))
        return CapturingCursor(self.collection.find(*args, **kwargs), self.capture)

    def aggregate(self, pipeline, *args, **kwargs):
        self.owner.query_count += 1
        self.capture.clear()
        self.capture.update(kind="aggregate", pipeline=pipeline)
        return self.collection.aggregate(pipeline, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.collection, name)


class CapturingDatabase:
    """代理一个真实数据库，仅把指定集合访问换成可记录的包装器。"""

    def __init__(self, database, collection_name: str) -> None:
        self.database, self.collection_name, self.capture, self.query_count = database, collection_name, {}, 0

    def __getitem__(self, name):
        collection = self.database[name]
        return CapturingCollection(collection, self.capture, self) if name == self.collection_name else collection

    def __getattr__(self, name):
        collection = getattr(self.database, name)
        return CapturingCollection(collection, self.capture, self) if name == self.collection_name else collection


async def explain_captured_cursor(db, collection: str, capture: dict) -> dict[str, object]:
    """将后端刚执行的真实 find 或 aggregate 描述原样交给 Mongo Explain。"""
    if capture.get("kind") == "find":
        sort = capture.get("sort", {})
        if isinstance(sort, list):
            sort = dict(sort)
        command = {
            "find": collection,
            "filter": capture["filter"],
            "sort": sort,
            "limit": capture.get("limit", 0),
        }
    elif capture.get("kind") == "aggregate":
        command = {"aggregate": collection, "cursor": {}, "pipeline": capture["pipeline"]}
    else:
        raise AssertionError("未捕获游标后端对目标事件集合的真实查询")
    value = await db.command("explain", command, verbosity="executionStats")
    stats = execution_stats(value)
    plan = value.get("queryPlanner", {}).get("winningPlan", value)
    return {
        "ixscan": has_stage(plan, "IXSCAN"),
        "sort": has_stage(plan, "SORT"),
        "returned": int(stats.get("nReturned", 0)),
        "keysExamined": int(stats.get("totalKeysExamined", 0)),
        "docsExamined": int(stats.get("totalDocsExamined", 0)),
        "planShape": plan_shape(plan),
    }


def item_ids(page: dict) -> list[str]:
    """只提取公开逻辑 ID，报告和断言均不输出事件原文或内部 BSON ID。"""
    return [str(item["id"]) for item in page["items"]]


async def audit_cursor_page(db, query: dict, *, cursor: str, page_size: int, include_total: bool = False) -> dict:
    """调用最终审计游标合同；显式空字符串请求游标首屏而非旧页码首屏。"""
    return await event_cursor_page(
        db, "audit", query, cursor, page_size, include_total=include_total,
    )


async def runtime_cursor_page(db, query: dict, *, cursor: str, page_size: int,
                              include_total: bool = False) -> dict:
    """调用最终运行事件游标合同，供规范时间和 legacy 有效时间路径共用。"""
    return await runtime_event_cursor_page(
        db, query, cursor, page_size, include_total=include_total,
    )


async def collect_pages(fetch, *, page_size: int) -> tuple[list[str], int]:
    """顺序消费 nextCursor，并检查游标响应没有精确总数或重复公开 ID。"""
    cursor, identifiers, pages = "", [], 0
    while True:
        page = await fetch(cursor)
        check(page.get("pageSize") == page_size, "游标响应 pageSize 与请求不一致")
        check(page.get("total") is None, "includeTotal=false 的游标读取不应计算总数")
        check(isinstance(page.get("hasMore"), bool), "游标响应缺少 hasMore 布尔值")
        current = item_ids(page)
        check(not (set(identifiers) & set(current)), "跨游标页面出现重复公开 ID")
        identifiers.extend(current)
        pages += 1
        if not page["hasMore"]:
            check(page.get("nextCursor") is None, "末页不能返回 nextCursor")
            return identifiers, pages
        check(isinstance(page.get("nextCursor"), str) and page["nextCursor"], "非末页缺少 nextCursor")
        cursor = page["nextCursor"]


async def expect_cursor_422(call, message: str) -> None:
    """游标跨集合或跨筛选条件复用必须被安全拒绝。"""
    try:
        await call()
    except HTTPException as error:
        check(error.status_code == 422, f"{message} 返回了非 422 状态")
        return
    raise AssertionError(message)


async def verify() -> dict[str, object]:
    """创建随机库执行游标与 Explain 验证，finally 无条件删除整个隔离数据库。"""
    configured = Settings()
    database_name = "event_cursor_verify_" + uuid4().hex
    client = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    try:
        hello = await client.admin.command("hello")
        if not hello.get("setName"):
            raise RuntimeError("管理事件游标验证需要 MongoDB 副本集")
        db = client[database_name]
        await install_event_indexes(db)
        stamp = datetime(2026, 9, 14, 8, tzinfo=UTC)

        # ObjectId 与字符串（包含看似 ObjectId 的 24 hex 字符串）同秒排序，验证游标
        # 保存/恢复 BSON 原类型后仍完整遍历。逻辑 id 仅用于验证输出，不参与排序。
        mixed_query = {"requestId": "cursor-mixed"}
        mixed_rows = [
            {"_id": ObjectId("64f000000000000000000001"), "id": "mixed-oid-a", **mixed_query, "action": "inspect", "createdAt": stamp},
            {"_id": ObjectId("64f000000000000000000002"), "id": "mixed-oid-b", **mixed_query, "action": "inspect", "createdAt": stamp},
            {"_id": "ffffffffffffffffffffffff", "id": "mixed-string-24-f", **mixed_query, "action": "inspect", "createdAt": stamp},
            {"_id": "0123456789abcdef01234567", "id": "mixed-string-24-0", **mixed_query, "action": "inspect", "createdAt": stamp},
            {"_id": "plain-string", "id": "mixed-string-plain", **mixed_query, "action": "inspect", "createdAt": stamp},
        ]
        mixed_rows.extend(
            {"_id": ObjectId(), "id": f"mixed-oid-bulk-{index:03d}", **mixed_query, "action": "inspect", "createdAt": stamp}
            for index in range(120)
        )
        mixed_rows.extend(
            {"_id": f"{index:024x}", "id": f"mixed-string-bulk-{index:03d}", **mixed_query, "action": "inspect", "createdAt": stamp}
            for index in range(120)
        )
        await db.audit.insert_many(mixed_rows)
        expected_mixed = [row["id"] async for row in db.audit.find(mixed_query).sort([("createdAt", -1), ("_id", -1)])]
        first_mixed = await audit_cursor_page(db, mixed_query, cursor="", page_size=25)
        mixed_ids, mixed_pages = await collect_pages(
            lambda cursor: audit_cursor_page(db, mixed_query, cursor=cursor, page_size=25), page_size=25,
        )
        check(mixed_ids == expected_mixed and len(mixed_ids) == len(set(mixed_ids)), "混合 BSON _id 同秒游标存在漏读、重读或排序漂移")

        counted = await audit_cursor_page(db, mixed_query, cursor="", page_size=25, include_total=True)
        check(counted.get("total") == len(expected_mixed), "includeTotal=true 未返回同筛选条件精确总数")
        foreign_cursor = first_mixed.get("nextCursor")
        check(isinstance(foreign_cursor, str) and foreign_cursor, "混合 _id 首屏未生成跨页游标")
        deep_cursor = foreign_cursor
        for _ in range(3):
            deep_page = await audit_cursor_page(db, mixed_query, cursor=deep_cursor, page_size=25)
            check(deep_page["hasMore"] and isinstance(deep_page.get("nextCursor"), str), "混合 ID 深页未生成后续游标")
            deep_cursor = deep_page["nextCursor"]
        await expect_cursor_422(
            lambda: event_cursor_page(db, "request_events", {}, foreign_cursor, 25, include_total=False),
            "审计游标跨集合复用未被拒绝",
        )
        await expect_cursor_422(
            lambda: audit_cursor_page(db, {"requestId": "other-filter"}, cursor=foreign_cursor, page_size=25),
            "审计游标跨筛选条件复用未被拒绝",
        )

        # 派生 PENDING 不依赖已持久化 outcome，须与展示器推导结果一起进入游标分页。
        pending_query = {"taskId": "cursor-pending"}
        await db.events.insert_many([
            {"id": "pending-debug", **pending_query, "type": "DEBUG_MODE", "phase": "STARTED", "createdAt": stamp + timedelta(minutes=1)},
            {"id": "pending-remount", **pending_query, "type": "COREDUMP_MOUNT", "status": "REMOUNTING", "createdAt": stamp + timedelta(minutes=2)},
            {"id": "not-pending", **pending_query, "type": "DEBUG_MODE", "phase": "ASH_READY", "createdAt": stamp + timedelta(minutes=3)},
        ])
        pending_ids, pending_pages = await collect_pages(
            lambda cursor: runtime_cursor_page(db, pending_query | {"outcome": "PENDING"}, cursor=cursor, page_size=1),
            page_size=1,
        )
        check(set(pending_ids) == {"pending-debug", "pending-remount"}, "派生 PENDING 游标筛选遗漏或包含非匹配事件")

        # legacy 行必须按 detectedAt 参与有效事件时间排序，不得退化为 createdAt 游标。
        legacy_query = {"taskId": "cursor-legacy"}
        await db.events.insert_many([
            {"id": "legacy-old", **legacy_query, "type": "CONNECTION_GAP", "detectedAt": stamp + timedelta(minutes=4)},
            {"id": "legacy-new", **legacy_query, "type": "CONNECTION_GAP", "createdAt": None, "detectedAt": stamp + timedelta(minutes=6)},
            {"id": "legacy-created", **legacy_query, "type": "CONNECTION_GAP", "createdAt": stamp + timedelta(minutes=5)},
        ])
        legacy_ids, legacy_pages = await collect_pages(
            lambda cursor: runtime_cursor_page(db, legacy_query, cursor=cursor, page_size=1), page_size=1,
        )
        check(legacy_ids == ["legacy-new", "legacy-created", "legacy-old"], "legacy detectedAt 游标未按有效事件时间完整排序")

        # 用第五页的真实 opaque cursor 执行一次查询并捕获实际 filter/pipeline；这里
        # 特意让所有 245 条记录同秒，迫使实现走同时间 `_id`（含 BSON 类型）边界。
        captured_db = CapturingDatabase(db, "audit")
        captured_page = await audit_cursor_page(captured_db, mixed_query, cursor=deep_cursor, page_size=25)
        deep_plan = await explain_captured_cursor(db, "audit", captured_db.capture)
        expected_candidates = len(captured_page["items"]) + int(captured_page["hasMore"])
        check(captured_db.query_count == 1, "目标游标页未执行唯一一次 find 或 aggregate 查询")
        check(deep_plan["returned"] == expected_candidates,
              "真实混合 ID 游标 Explain 返回量与实际页候选数不一致")
        check(not deep_plan["sort"], f"真实混合 ID 游标条件触发阻塞排序: {deep_plan}")
        check(deep_plan["keysExamined"] >= deep_plan["returned"] and deep_plan["docsExamined"] >= deep_plan["returned"],
              "真实混合 ID 游标 Explain 缺少有效扫描统计")
        return {
            "passed": True,
            "temporaryDatabase": database_name,
            "mixedIdPages": mixed_pages,
            "mixedIdCount": len(mixed_ids),
            "pendingPages": pending_pages,
            "legacyPages": legacy_pages,
            "mixedIdCursorCandidates": expected_candidates,
            "mixedIdCursorVisible": len(captured_page["items"]),
            "mixedIdCursorQueries": captured_db.query_count,
            "mixedIdCursorPlan": deep_plan,
            "noDeviceOrLogAccess": True,
        }
    finally:
        await client.drop_database(database_name)
        removed = database_name not in await client.list_database_names()
        await client.close()
        if not removed:
            raise AssertionError("随机管理事件游标验证数据库未被删除")


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False, sort_keys=True))
