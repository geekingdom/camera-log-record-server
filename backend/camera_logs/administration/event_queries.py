"""提供管理事件的共享分页、时间过滤和展示字段一致的数据库筛选。"""

import inspect

from camera_logs.administration.event_presenter import ACTION_OUTCOMES, present_events
from camera_logs.common.database import public


def split_derived_filters(query: dict) -> tuple[dict, dict]:
    """分离历史文档可能缺失的结果和级别条件，供展示推导后统一过滤。"""
    database_query = dict(query)
    derived = {name: database_query.pop(name) for name in ("level", "outcome") if name in database_query}
    return database_query, derived


_OUTCOMES = ["PENDING", "SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"]
_PRESSURE_EVENTS = ["DISK_PRESSURE_CHANGED", "WRITE_PRESSURE_CHANGED"]


def _derived_fields() -> dict:
    """构造与展示器相同的 Mongo 结果和级别表达式，避免旧记录筛选语义漂移。"""
    outcome = {"$switch": {"branches": [
        {"case": {"$in": ["$outcome", _OUTCOMES]}, "then": "$outcome"},
        *[{"case": {"$eq": ["$action", action]}, "then": result} for action, result in ACTION_OUTCOMES.items()],
        {"case": {"$eq": ["$responseComplete", False]}, "then": "UNKNOWN"},
        {"case": {"$in": ["$status", _OUTCOMES]}, "then": "$status"},
        {"case": {"$eq": ["$httpStatus", 202]}, "then": "PENDING"},
        {"case": {"$gte": ["$httpStatus", 400]}, "then": "FAILED"},
        {"case": {"$in": ["$type", ["CONNECTION_GAP", "CLOCK_ROLLBACK"]]}, "then": "UNKNOWN"},
        {"case": {"$in": ["$type", _PRESSURE_EVENTS]}, "then": {
            "$cond": [{"$eq": ["$level", "NORMAL"]}, "SUCCEEDED", "UNKNOWN"]}},
        {"case": {"$eq": ["$type", "DEBUG_MODE"]}, "then": {
            "$cond": [{"$in": [{"$ifNull": ["$debugError", None]}, [None, False, 0, ""]]}, "SUCCEEDED", "FAILED"]}},
    ], "default": "SUCCEEDED"}}
    level = {"$switch": {"branches": [
        {"case": {"$in": ["$type", _PRESSURE_EVENTS]}, "then": {"$switch": {"branches": [
            {"case": {"$eq": ["$level", "CRITICAL"]}, "then": "ERROR"},
            {"case": {"$eq": ["$level", "NORMAL"]}, "then": "INFO"},
        ], "default": "WARNING"}}},
        {"case": {"$in": ["$level", ["DEBUG", "INFO", "WARNING", "ERROR"]]}, "then": "$level"},
        {"case": {"$eq": ["$_derivedOutcome", "FAILED"]}, "then": "ERROR"},
        {"case": {"$in": ["$_derivedOutcome", ["UNKNOWN", "CANCELLED"]]}, "then": "WARNING"},
    ], "default": "INFO"}}
    # Mongo 同一 $addFields 阶段的表达式互不可见；级别必须在下一阶段读取结果。
    return {"_derivedOutcome": outcome}, {"_derivedLevel": level}


async def _aggregate(db, collection: str, pipeline: list[dict]) -> list[dict]:
    """适配生产异步 PyMongo 聚合游标并返回当前查询阶段的有限结果。"""
    cursor = db[collection].aggregate(pipeline)
    if inspect.isawaitable(cursor):
        cursor = await cursor
    return [item async for item in cursor]


async def _derived_page(db, collection: str, database_query: dict, derived: dict, page: int,
                        page_size: int, *, event_time: bool = False) -> dict:
    """在 Mongo 内推导、过滤与计数；聚合结果只读取当前页面的至多 100 条事件。"""
    outcome_fields, level_fields = _derived_fields()
    if event_time:
        level_fields["_eventTime"] = {"$ifNull": ["$createdAt", "$detectedAt"]}
    match = {"_derivedLevel" if name == "level" else "_derivedOutcome": value for name, value in derived.items()}
    prefix = [{"$match": database_query}, {"$addFields": outcome_fields}, {"$addFields": level_fields}, {"$match": match}]
    sort = {"_eventTime": -1, "_id": -1} if event_time else {"createdAt": -1, "_id": -1}
    page_pipeline = prefix + [{"$sort": sort}, {"$skip": (page - 1) * page_size}, {"$limit": page_size},
                              {"$project": {"_derivedOutcome": 0, "_derivedLevel": 0, "_eventTime": 0}}]
    count_pipeline = prefix + [{"$count": "total"}]
    items, count_rows = await _aggregate(db, collection, page_pipeline), await _aggregate(db, collection, count_pipeline)
    if event_time:
        for item in items:
            if item.get("createdAt") is None and item.get("detectedAt") is not None:
                item["createdAt"] = item["detectedAt"]
    return {"items": await present_events(db, [public(item) for item in items]),
            "total": count_rows[0]["total"] if count_rows else 0, "page": page, "pageSize": page_size}


async def event_page(db, collection, query, page, page_size):
    """普通条件走索引分页；展示派生条件在 Mongo 内完成筛选和计数。"""
    database_query, derived = split_derived_filters(query)
    if derived:
        return await _derived_page(db, collection, database_query, derived, page, page_size)
    cursor = db[collection].find(database_query).sort([("createdAt", -1), ("_id", -1)]).skip(
        (page - 1) * page_size).limit(page_size)
    return {"items": await present_events(db, [public(item) async for item in cursor]),
            "total": await db[collection].count_documents(database_query), "page": page, "pageSize": page_size}


async def runtime_event_page(db, query, page, page_size):
    """兼容 detectedAt 历史运行事件；所有路径均在数据库内排序分页。"""
    database_query, derived = split_derived_filters(query)
    if derived:
        return await _derived_page(db, "events", database_query, derived, page, page_size, event_time=True)
    items = await _aggregate(db, "events", [
        {"$match": database_query}, {"$addFields": {"_eventTime": {"$ifNull": ["$createdAt", "$detectedAt"]}}},
        {"$sort": {"_eventTime": -1, "_id": -1}}, {"$skip": (page - 1) * page_size},
        {"$limit": page_size}, {"$project": {"_eventTime": 0}},
    ])
    for item in items:
        if item.get("createdAt") is None and item.get("detectedAt") is not None:
            item["createdAt"] = item["detectedAt"]
    return {"items": await present_events(db, [public(item) for item in items]),
            "total": await db.events.count_documents(database_query), "page": page, "pageSize": page_size}
