"""资源 Coredump 最后已知存在状态的聚合查询。"""

from __future__ import annotations

import inspect
from typing import Any

FLAG_FILE_PATTERN = r"(^|/)coredump_flag[.]cdf$"


def visible_coredump_query() -> dict[str, Any]:
    """返回与 Coredump 列表一致的可见目录条件，兼容升级前未写存在状态的记录。"""
    return {
        "$nor": [{"name": {"$regex": FLAG_FILE_PATTERN, "$options": "i"}}],
        "$or": [
            {"sourcePresent": {"$ne": False}},
            {"status": "FROZEN", "snapshot.path": {"$exists": True, "$ne": None}},
            {"status": "FROZEN", "snapshot.reservationToken": {"$exists": True, "$ne": None}},
        ],
    }


def resource_presence_pipeline(
    query: dict[str, Any], page: int, page_size: int, has_coredumps: bool
) -> list[dict[str, Any]]:
    """在资源候选集内以每项至多一个 catalog 条目判断存在性，再执行分页。"""
    coredump_match = visible_coredump_query()
    return [
        {"$match": query},
        {
            "$lookup": {
                "from": "coredump_files",
                "let": {"resourceId": "$id"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$resourceId", "$$resourceId"]}}},
                    {"$match": coredump_match},
                    {"$project": {"_id": 1}},
                    {"$limit": 1},
                ],
                "as": "_coredumpPresence",
            }
        },
        {"$set": {"hasCoredumps": {"$gt": [{"$size": "$_coredumpPresence"}, 0]}}},
        {"$match": {"hasCoredumps": has_coredumps}},
        {"$sort": {"createdAt": -1, "id": 1}},
        {
            "$facet": {
                "items": [
                    {"$skip": (page - 1) * page_size},
                    {"$limit": page_size},
                    {"$project": {"_coredumpPresence": 0}},
                ],
                "total": [{"$count": "value"}],
            }
        },
    ]


async def page_resources_by_presence(
    db: Any, query: dict[str, Any], page: int, page_size: int, has_coredumps: bool
) -> dict[str, Any]:
    """执行存在性关联；总数和页项目共用同一筛选快照。"""
    cursor = db.resources.aggregate(resource_presence_pipeline(query, page, page_size, has_coredumps))
    if inspect.isawaitable(cursor):
        cursor = await cursor
    rows = [item async for item in cursor]
    row = rows[0] if rows else {"items": [], "total": []}
    return {
        "items": row["items"],
        "total": row["total"][0]["value"] if row["total"] else 0,
        "page": page,
        "pageSize": page_size,
    }


async def coredump_presence_by_resource_ids(db: Any, identifiers: list[str]) -> set[str]:
    """对当前资源页一次分组，避免资源列表按行查询 Coredump 目录。"""
    if not identifiers:
        return set()
    query = {"$and": [{"resourceId": {"$in": identifiers}}, visible_coredump_query()]}
    cursor = db.coredump_files.aggregate([{"$match": query}, {"$group": {"_id": "$resourceId"}}])
    if inspect.isawaitable(cursor):
        cursor = await cursor
    return {item["_id"] async for item in cursor}
