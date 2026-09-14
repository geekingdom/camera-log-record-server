"""提供管理事件的共享分页、时间过滤和展示字段一致的数据库筛选。"""

import inspect

from camera_logs.administration.event_cursor import (
    after_event_cursor_clause,
    decode_event_cursor,
    encode_event_cursor,
)
from camera_logs.administration.event_presenter import (
    ACTION_OUTCOMES,
    COREDUMP_MOUNT_OUTCOMES,
    DEBUG_MODE_OUTCOMES,
    present_events,
)
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
        {"case": {"$eq": ["$type", "COREDUMP_MOUNT"]}, "then": {"$switch": {"branches": [
            {"case": {"$eq": ["$status", status]}, "then": result}
            for status, result in COREDUMP_MOUNT_OUTCOMES.items()
        ], "default": "SUCCEEDED"}}},
        {"case": {"$in": ["$type", ["CONNECTION_GAP", "CLOCK_ROLLBACK", "IDLE_TIMEOUT", "READ_ERROR"]]},
         "then": "UNKNOWN"},
        {"case": {"$in": ["$type", _PRESSURE_EVENTS]}, "then": {
            "$cond": [{"$eq": ["$level", "NORMAL"]}, "SUCCEEDED", "UNKNOWN"]}},
        {"case": {"$eq": ["$type", "DEBUG_MODE"]}, "then": {"$switch": {"branches": [
            {"case": {"$eq": ["$phase", phase]}, "then": result}
            for phase, result in DEBUG_MODE_OUTCOMES.items()
        ], "default": {"$cond": [
            {"$in": [{"$ifNull": ["$debugError", None]}, [None, False, 0, ""]]}, "SUCCEEDED", "FAILED"
        ]}}}},
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
    # 持久时间排序放在派生字段前，Mongo可直接按索引有序读取；过滤后才允许skip/limit。
    # 旧事件的_eventTime需要先计算，迁移未完成时继续保留原来的兼容排序。
    ordered = (prefix + [{"$sort": sort}] if event_time else
               [prefix[0], {"$sort": sort}, *prefix[1:]])
    page_pipeline = ordered + [{"$skip": (page - 1) * page_size}, {"$limit": page_size},
                              {"$project": {"_derivedOutcome": 0, "_derivedLevel": 0, "_eventTime": 0}}]
    count_pipeline = prefix + [{"$count": "total"}]
    items, count_rows = await _aggregate(db, collection, page_pipeline), await _aggregate(db, collection, count_pipeline)
    if event_time:
        for item in items:
            if item.get("createdAt") is None and item.get("detectedAt") is not None:
                item["createdAt"] = item["detectedAt"]
    return {"items": await present_events(db, [public(item) for item in items]),
            "total": count_rows[0]["total"] if count_rows else 0, "page": page, "pageSize": page_size}


def _and_clause(query: dict, clause: dict | None) -> dict:
    """不覆盖已有 ``$or`` 条件地追加游标范围，保持调用方原筛选语义。"""
    if clause is None:
        return query
    return {"$and": [query, clause]}


async def _present_cursor_page(db, collection: str, query: dict, *, legacy_time: bool, page_size: int,
                               include_total: bool, items: list[dict], count_pipeline: list[dict] | None = None) -> dict:
    """从原始文档生成游标后再脱敏展示，避免 ``public`` 丢失 BSON ``_id``。"""
    has_more = len(items) > page_size
    visible = items[:page_size]
    next_cursor = None
    if has_more and visible:
        last = visible[-1]
        event_time = last.get("_eventTime") if legacy_time else last.get("createdAt")
        next_cursor = encode_event_cursor(
            collection=collection, query=query, legacy_time=legacy_time,
            event_time=event_time, identifier=last.get("_id"),
        )
    for item in visible:
        if legacy_time and item.get("createdAt") is None and item.get("_eventTime") is not None:
            item["createdAt"] = item["_eventTime"]
        item.pop("_eventTime", None)
        item.pop("_derivedOutcome", None)
        item.pop("_derivedLevel", None)
    if include_total:
        if count_pipeline is None:
            total = await db[collection].count_documents(split_derived_filters(query)[0])
        else:
            rows = await _aggregate(db, collection, count_pipeline)
            total = rows[0]["total"] if rows else 0
    else:
        total = None
    return {
        "items": await present_events(db, [public(item) for item in visible]),
        "pageSize": page_size,
        "hasMore": has_more,
        "nextCursor": next_cursor,
        "total": total,
    }


async def event_cursor_page(db, collection: str, query: dict, cursor: str, page_size: int, *,
                            include_total: bool = False) -> dict:
    """按 ``createdAt,_id`` 续读普通管理事件，默认避免 count 与深层 skip。"""
    anchor = decode_event_cursor(cursor, collection=collection, query=query, legacy_time=False) if cursor else None
    database_query, derived = split_derived_filters(query)
    clause = after_event_cursor_clause(time_field="createdAt", event_time=anchor.event_time,
                                       identifier=anchor.identifier) if anchor else None
    filtered = _and_clause(database_query, clause)
    if not derived:
        items = [item async for item in db[collection].find(filtered).sort(
            [("createdAt", -1), ("_id", -1)]).limit(page_size + 1)]
        return await _present_cursor_page(db, collection, query, legacy_time=False, page_size=page_size,
                                          include_total=include_total, items=items)

    outcome_fields, level_fields = _derived_fields()
    match = {"_derivedLevel" if name == "level" else "_derivedOutcome": value for name, value in derived.items()}
    prefix = [{"$match": filtered}, {"$sort": {"createdAt": -1, "_id": -1}},
              {"$addFields": outcome_fields}, {"$addFields": level_fields}, {"$match": match}]
    items = await _aggregate(db, collection, prefix + [{"$limit": page_size + 1}])
    count_pipeline = ([{"$match": database_query}, {"$addFields": outcome_fields},
                       {"$addFields": level_fields}, {"$match": match}, {"$count": "total"}]
                      if include_total else None)
    return await _present_cursor_page(db, collection, query, legacy_time=False, page_size=page_size,
                                      include_total=include_total, items=items, count_pipeline=count_pipeline)


async def runtime_event_cursor_page(db, query: dict, cursor: str, page_size: int, *, time_range=None,
                                    include_total: bool = False) -> dict:
    """按实际事件时间续读运行事件，兼容旧 ``detectedAt`` 记录且不改变旧页码接口。"""
    legacy = await db.events.find_one({"createdAt": None, "detectedAt": {"$ne": None}}, {"_id": 1})
    database_query = dict(query)
    if time_range:
        if legacy is None:
            database_query["createdAt"] = time_range
        else:
            database_query["$or"] = [{"createdAt": time_range}, {"createdAt": None, "detectedAt": time_range}]
    if legacy is None:
        return await event_cursor_page(db, "events", database_query, cursor, page_size, include_total=include_total)

    anchor = decode_event_cursor(cursor, collection="events", query=database_query, legacy_time=True) if cursor else None
    source_query, derived = split_derived_filters(database_query)
    outcome_fields, level_fields = _derived_fields()
    cursor_clause = after_event_cursor_clause(time_field="_eventTime", event_time=anchor.event_time,
                                              identifier=anchor.identifier) if anchor else None
    match = {"_derivedLevel" if name == "level" else "_derivedOutcome": value for name, value in derived.items()}
    prefix = [{"$match": source_query}, {"$addFields": {"_eventTime": {"$ifNull": ["$createdAt", "$detectedAt"]}}}]
    if cursor_clause:
        prefix.append({"$match": cursor_clause})
    pipeline = prefix + [{"$sort": {"_eventTime": -1, "_id": -1}}]
    if derived:
        pipeline.extend([{ "$addFields": outcome_fields}, {"$addFields": level_fields}, {"$match": match}])
    items = await _aggregate(db, "events", pipeline + [{"$limit": page_size + 1}])
    count_pipeline = None
    if include_total and derived:
        count_pipeline = ([{"$match": source_query}, {"$addFields": outcome_fields},
                           {"$addFields": level_fields}, {"$match": match}, {"$count": "total"}])
    return await _present_cursor_page(db, "events", database_query, legacy_time=True, page_size=page_size,
                                      include_total=include_total, items=items, count_pipeline=count_pipeline)


async def event_page(db, collection, query, page, page_size):
    """普通条件走索引分页；展示派生条件在 Mongo 内完成筛选和计数。"""
    database_query, derived = split_derived_filters(query)
    if derived:
        return await _derived_page(db, collection, database_query, derived, page, page_size)
    cursor = db[collection].find(database_query).sort([("createdAt", -1), ("_id", -1)]).skip(
        (page - 1) * page_size).limit(page_size)
    return {"items": await present_events(db, [public(item) async for item in cursor]),
            "total": await db[collection].count_documents(database_query), "page": page, "pageSize": page_size}


async def runtime_event_page(db, query, page, page_size, *, time_range=None):
    """旧时间未迁移时兼容读取；规范化后使用createdAt索引，不缓存迁移完成判断。"""
    # 默认时间索引可定位null/缺失项；每次检查已提交的旧格式，不缓存迁移完成状态。
    # 探测和分页不是同一快照，部署应先升级全部写入端，避免旧格式在两次读取间插入。
    # 不使用持久迁移标记；迁移并发只会将旧格式变成等价新格式，不改变排序时间。
    legacy = await db.events.find_one({"createdAt": None, "detectedAt": {"$ne": None}}, {"_id": 1})
    query = dict(query)
    if time_range:
        if legacy is None:
            query["createdAt"] = time_range
        else:
            query["$or"] = [{"createdAt": time_range}, {"createdAt": None, "detectedAt": time_range}]
    if legacy is None:
        return await event_page(db, "events", query, page, page_size)
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
