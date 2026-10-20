"""资源工作台的全量筛选、派生任务摘要和稳定排序。"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import HTTPException

from camera_logs.coredumps.presence import visible_coredump_query

RESOURCE_SORT_FIELDS = {
    "identity": "name",
    "ip": "ip",
    "deviceIdentity": "model",
    "health": "healthStatus",
    "tasks": "taskCount",
    "creator": "_creatorSort",
    "createdAt": "createdAt",
    "deletedAt": "deletedAt",
}


def resource_sort(sort_by: str | None, sort_order: str | None) -> dict[str, int]:
    """解析资源表排序参数；空参数严格回到既有创建时间倒序。"""
    if sort_by is None and sort_order is None:
        return {"createdAt": -1, "id": 1}
    if sort_by not in RESOURCE_SORT_FIELDS or sort_order not in {"asc", "desc"}:
        raise HTTPException(422, "资源排序字段或方向无效")
    direction = 1 if sort_order == "asc" else -1
    if sort_by == "identity":
        return {"name": direction, "model": direction, "subSerialNumber": direction, "id": 1}
    if sort_by == "deviceIdentity":
        return {"model": direction, "subSerialNumber": direction, "softwareVersion": direction, "id": 1}
    return {RESOURCE_SORT_FIELDS[sort_by]: direction, "id": 1}


def _task_summary_lookup() -> dict[str, Any]:
    """按资源 ID 在数据库内聚合任务数量，避免把任务正文数组拼入资源管道。"""
    return {"$lookup": {"from": "tasks", "let": {"resourceId": "$id"}, "pipeline": [
        {"$match": {"$and": [{"deletedAt": None}, {"$expr": {"$or": [
            {"$eq": ["$resourceId", "$$resourceId"]}, {"$eq": ["$serialServerResourceId", "$$resourceId"]},
        ]}}]}},
        {"$group": {"_id": None, "taskCount": {"$sum": 1},
            "activeTaskCount": {"$sum": {"$cond": [{"$and": [
                {"$eq": ["$status", "COLLECTING"]}, {"$eq": ["$desiredState", "RUNNING"]},
            ]}, 1, 0]}},
            "unsettledTaskCount": {"$sum": {"$cond": [{"$or": [
                {"$ne": [{"$ifNull": ["$nodeId", None]}, None]},
                {"$in": ["$desiredState", ["RUNNING", "PAUSED"]]}, {"$eq": ["$status", "BLOCKED"]},
            ]}, 1, 0]}}}},
    ], "as": "_taskSummary"}}


def _summary_fields() -> dict[str, Any]:
    """将至多一条任务分组结果展开为资源展示与排序字段。"""
    return {"$set": {
        "_creatorSort": {"$ifNull": ["$createdByName", "$createdBy"]},
        "taskCount": {"$ifNull": [{"$arrayElemAt": ["$_taskSummary.taskCount", 0]}, 0]},
        "activeTaskCount": {"$ifNull": [{"$arrayElemAt": ["$_taskSummary.activeTaskCount", 0]}, 0]},
        "unsettledTaskCount": {"$ifNull": [{"$arrayElemAt": ["$_taskSummary.unsettledTaskCount", 0]}, 0]},
    }}


def resource_list_pipeline(query: dict[str, Any], page: int, page_size: int, *, has_coredumps: bool | None,
                           sort_by: str | None, sort_order: str | None) -> list[dict[str, Any]]:
    """仅派生任务数排序时才全量关联计数，普通字段排序先分页。"""
    pipeline: list[dict[str, Any]] = [{"$match": query}]
    if sort_by == "creator":
        pipeline.append({"$set": {"_creatorSort": {"$ifNull": ["$createdByName", "$createdBy"]}}})
    if sort_by == "tasks":
        pipeline.extend([_task_summary_lookup(), _summary_fields()])
    if has_coredumps is not None:
        # 仅筛选时关联目录。未筛选列表在分页后批量补齐，避免常规请求把所有
        # Core 文件带入聚合；筛选必须在排序分页前完成。
        pipeline.extend([
            {"$lookup": {"from": "coredump_files", "let": {"resourceId": "$id"}, "pipeline": [
                {"$match": {"$expr": {"$eq": ["$resourceId", "$$resourceId"]}}},
                {"$match": visible_coredump_query()}, {"$limit": 1},
            ], "as": "_coredumpPresence"}},
            {"$set": {"hasCoredumps": {"$gt": [{"$size": "$_coredumpPresence"}, 0]}}},
            {"$match": {"hasCoredumps": has_coredumps}},
        ])
    pipeline.extend([
        {"$sort": resource_sort(sort_by, sort_order)},
        {"$facet": {
            "items": [{"$skip": (page - 1) * page_size}, {"$limit": page_size},
                      {"$project": {"_resourceTasks": 0, "_serialTasks": 0, "_tasks": 0,
                                    "_coredumpPresence": 0, "_taskSummary": 0, "_creatorSort": 0}}],
            "total": [{"$count": "value"}],
        }},
    ])
    return pipeline


async def page_resources(db: Any, query: dict[str, Any], page: int, page_size: int, *, has_coredumps: bool | None,
                         sort_by: str | None, sort_order: str | None) -> dict[str, Any]:
    """执行单次聚合分页，计数与页面来自同一个筛选和排序快照。"""
    sort = resource_sort(sort_by, sort_order)
    if sort_by not in {"tasks", "creator"} and has_coredumps is None:
        cursor = db.resources.find(query).sort(list(sort.items())).skip((page - 1) * page_size).limit(page_size)
        items = [item async for item in cursor]
        await _add_task_summaries(db, items)
        return {"items": items, "total": await db.resources.count_documents(query), "page": page, "pageSize": page_size}
    cursor = db.resources.aggregate(resource_list_pipeline(
        query, page, page_size, has_coredumps=has_coredumps, sort_by=sort_by, sort_order=sort_order,
    ))
    if inspect.isawaitable(cursor):
        cursor = await cursor
    rows = [row async for row in cursor]
    row = rows[0] if rows else {"items": [], "total": []}
    items = row["items"]
    if sort_by != "tasks":
        await _add_task_summaries(db, items)
    return {"items": items, "total": row["total"][0]["value"] if row["total"] else 0,
            "page": page, "pageSize": page_size}


async def _add_task_summaries(db: Any, resources: list[dict[str, Any]]) -> None:
    """为已分页资源做一次分组统计，不读取或传输任务正文。"""
    identifiers = [resource["id"] for resource in resources]
    if not identifiers:
        return
    cursor = db.tasks.aggregate([
        {"$match": {"deletedAt": None, "$or": [{"resourceId": {"$in": identifiers}}, {"serialServerResourceId": {"$in": identifiers}}]}},
        {"$project": {"id": 1, "resourceId": 1, "serialServerResourceId": 1, "status": 1, "desiredState": 1, "nodeId": 1}},
    ])
    if inspect.isawaitable(cursor):
        cursor = await cursor
    summaries = {identifier: {"taskCount": 0, "activeTaskCount": 0, "unsettledTaskCount": 0} for identifier in identifiers}
    async for task in cursor:
        # 同一任务双绑定同一资源时只计一次，和 task_resource_query 的 $or 语义一致。
        targets = {value for value in (task.get("resourceId"), task.get("serialServerResourceId")) if value in summaries}
        for identifier in targets:
            summary = summaries[identifier]
            summary["taskCount"] += 1
            summary["activeTaskCount"] += task.get("status") == "COLLECTING" and task.get("desiredState") == "RUNNING"
            summary["unsettledTaskCount"] += (task.get("nodeId") is not None or task.get("desiredState") in {"RUNNING", "PAUSED"}
                                              or task.get("status") == "BLOCKED")
    for resource in resources:
        resource.update(summaries[resource["id"]])
