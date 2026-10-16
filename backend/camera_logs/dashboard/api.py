"""按上海自然日汇总资源、任务、已登记日志目录和 Coredump 目录。"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from datetime import date as CalendarDate
from time import monotonic
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Query

from camera_logs.common.database import now
from camera_logs.common.presentation_time import utc_iso
from camera_logs.common.security import actor, authorize
from camera_logs.coredumps.presence import visible_coredump_query
from camera_logs.node.health import node_health

SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_TIME_MS = 3_000
_RANK_LIMIT = 10
_REQUEST_TIMEOUT_SECONDS = 4
_CACHE_SECONDS = 10
_MAX_CACHE_ENTRIES = 32


@dataclass(slots=True)
class _CacheEntry:
    """同一窗口的共享读取及其失效时间，未完成任务也必须有硬超时。"""

    expires_at: float
    task: asyncio.Task


def _window(selected: CalendarDate, days: int) -> tuple[datetime, datetime, CalendarDate]:
    """将上海自然日转换为半开 UTC 查询范围，窗口含选定日共固定天数。"""
    start_day = selected - timedelta(days=days - 1)
    lower = datetime.combine(start_day, time.min, SHANGHAI).astimezone(UTC)
    upper = datetime.combine(selected + timedelta(days=1), time.min, SHANGHAI).astimezone(UTC)
    return lower, upper, start_day


async def _aggregate(collection: Any, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """执行受时限保护的目录聚合，兼容测试替身的同步返回游标。"""
    cursor = collection.aggregate(pipeline, maxTimeMS=_MAX_TIME_MS)
    if inspect.isawaitable(cursor):
        cursor = await cursor
    return [item async for item in cursor]


async def _documents(collection: Any, query: dict[str, Any], projection: dict[str, Any]) -> list[dict[str, Any]]:
    """读取小型目录投影，避免看板把密码、路径或正文带入内存。"""
    cursor = collection.find(query, projection, max_time_ms=_MAX_TIME_MS)
    return [item async for item in cursor]


def _local_day(value: Any) -> str | None:
    """把日志目录的 UTC 小时字段还原为上海日期，旧异常值不会污染其它桶。"""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(SHANGHAI).date().isoformat()


def _iso(value: Any) -> str | None:
    """统一输出有时区的 ISO 时间，缺失或历史异常时间保持空值。"""
    if not isinstance(value, datetime):
        return None
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).isoformat()


def _file_match(task_ids: list[str], lower: datetime, upper: datetime) -> dict[str, Any]:
    """只匹配调用方可读任务的有效目录记录，绝不查询日志正文或压缩包路径。"""
    return {
        "taskId": {"$in": task_ids},
        "hour": {"$gte": utc_iso(lower), "$lt": utc_iso(upper)},
        "status": {"$in": ["OPEN", "READY"]},
        "bytes": {"$gt": 0},
    }


async def _log_statistics(repo: Any, tasks: list[dict[str, Any]], resources: dict[str, dict[str, Any]],
                          lower: datetime, upper: datetime, start_day: CalendarDate,
                          selected: CalendarDate, days: int) -> dict[str, Any]:
    """按原始分卷目录累计日志量；共享 tar 的 archiveBytes 从不参与该统计。"""
    task_ids = [task["id"] for task in tasks]
    empty_days = {
        (start_day + timedelta(days=index)).isoformat(): {"bytes": 0, "fileCount": 0}
        for index in range(days)
    }
    if not task_ids:
        return {"daily": [{"date": day, **values} for day, values in empty_days.items()],
                "topResources": [], "topResourcesTruncated": False}
    range_match = _file_match(task_ids, lower, upper)
    selected_lower, selected_upper, _ = _window(selected, 1)
    rows, resource_rows = await asyncio.gather(
        _aggregate(repo.db.files, [
            {"$match": range_match},
            {"$group": {"_id": "$hour", "bytes": {"$sum": "$bytes"}, "fileCount": {"$sum": 1}}},
        ]),
        _aggregate(repo.db.files, [
            {"$match": _file_match(task_ids, selected_lower, selected_upper)},
            {"$group": {"_id": "$taskId", "bytes": {"$sum": "$bytes"}, "fileCount": {"$sum": 1}}},
        ]),
    )
    for row in rows:
        day = _local_day(row.get("_id"))
        if day in empty_days:
            empty_days[day]["bytes"] += int(row.get("bytes", 0))
            empty_days[day]["fileCount"] += int(row.get("fileCount", 0))
    resource_by_task = {task["id"]: task.get("resourceId") for task in tasks}
    ranked: dict[str, dict[str, Any]] = {}
    for row in resource_rows:
        resource_id = resource_by_task.get(row.get("_id"))
        resource = resources.get(resource_id)
        if resource is None:
            continue
        item = ranked.setdefault(resource_id, {"resourceId": resource_id, "name": resource.get("name") or resource_id,
                                                "ip": resource.get("ip"), "bytes": 0, "fileCount": 0})
        item["bytes"] += int(row.get("bytes", 0))
        item["fileCount"] += int(row.get("fileCount", 0))
    ordered = sorted(ranked.values(), key=lambda item: (-item["bytes"], item["resourceId"]))
    return {"daily": [{"date": day, **values} for day, values in empty_days.items()],
            "topResources": ordered[:_RANK_LIMIT], "topResourcesTruncated": len(ordered) > _RANK_LIMIT}


async def _coredump_statistics(repo: Any, resources: dict[str, dict[str, Any]],
                               lower: datetime, upper: datetime) -> dict[str, Any]:
    """汇总选定日登记且仍可见的 Coredump 目录条目，不访问 NFS 原文。"""
    match = {"$and": [{"receivedAt": {"$gte": lower, "$lt": upper}}, visible_coredump_query()]}
    rows = await _aggregate(repo.db.coredump_files, [
        {"$match": match},
        {"$group": {"_id": "$resourceId", "fileCount": {"$sum": 1}, "bytes": {"$sum": "$size"},
                    "lastReceivedAt": {"$max": "$receivedAt"}}},
    ])
    items = []
    for row in rows:
        resource_id = row.get("_id")
        resource = resources.get(resource_id)
        if resource is None:
            continue
        items.append({"resourceId": resource_id, "name": resource.get("name") or resource_id,
                      "ip": resource.get("ip"), "fileCount": int(row.get("fileCount", 0)),
                      "bytes": int(row.get("bytes", 0)), "lastReceivedAt": _iso(row.get("lastReceivedAt"))})
    items.sort(key=lambda item: (-item["bytes"], item["resourceId"]))
    return {"resourceCount": len(items), "fileCount": sum(item["fileCount"] for item in items),
            "bytes": sum(item["bytes"] for item in items), "items": items[:_RANK_LIMIT],
            "truncated": len(items) > _RANK_LIMIT}


async def _nodes(repo: Any, *, is_admin: bool, assessed_at: datetime) -> tuple[int | None, list[dict[str, Any]] | None]:
    """节点仅管理员可见；输出健康摘要而非地址、令牌或完整运行配置。"""
    if not is_admin:
        return None, None
    documents = [node async for node in repo.db.nodes.find({"deletedAt": None}, max_time_ms=_MAX_TIME_MS)]
    items = []
    for node in documents:
        health = node_health(node, assessed_at)
        items.append({"id": node["id"], "name": node.get("name") or node["id"],
                      "healthLevel": health["status"], "reasons": health["reasons"],
                      "accepting": bool(node.get("accepting", False)) and health["status"] != "OFFLINE",
                      "activeTasks": int(node.get("activeTasks", 0)),
                      "capacity": node.get("capacity"), "inputBytesPerSecond": node.get("inputBytesPerSecond"),
                      "lastHeartbeat": _iso(node.get("heartbeat"))})
    return len(items), sorted(items, key=lambda item: item["id"])


async def _dashboard_payload(repo: Any, user: dict[str, Any], *, days: int, selected: CalendarDate,
                             lower: datetime, upper: datetime, start_day: CalendarDate) -> dict[str, Any]:
    """构造同一时点的看板响应；当前概览排除软删除，历史目录仍保留原资源归属。"""
    is_admin = bool(user.get("isAdmin") or "*" in user.get("scopes", []) or "admin" in user.get("scopes", []))
    generated_at = now()
    tasks, resource_documents, node_result = await asyncio.gather(
        _documents(repo.db.tasks, {}, {"id": 1, "resourceId": 1, "desiredState": 1, "status": 1,
                                       "deletedAt": 1, "resourceDeleted": 1}),
        _documents(repo.db.resources, {}, {"id": 1, "name": 1, "ip": 1, "healthStatus": 1, "deletedAt": 1}),
        _nodes(repo, is_admin=is_admin, assessed_at=generated_at),
    )
    resources = {resource["id"]: resource for resource in resource_documents}
    node_count, nodes = node_result
    active_resources = [resource for resource in resources.values() if resource.get("deletedAt") is None]
    current_tasks = [task for task in tasks if task.get("deletedAt") is None and not task.get("resourceDeleted")]
    overview = {"resources": len(active_resources),
                "onlineResources": sum(resource.get("healthStatus") == "ONLINE" for resource in active_resources),
                "collectingTasks": sum(task.get("desiredState") == "RUNNING" and task.get("status") == "COLLECTING" for task in current_tasks),
                "tasks": len(current_tasks), "nodes": node_count}
    can_read_logs = "*" in user.get("scopes", []) or "logs:read" in user.get("scopes", [])
    response: dict[str, Any] = {"generatedAt": generated_at.isoformat(), "date": selected.isoformat(), "days": days,
                                "range": {"startDate": start_day.isoformat(), "endDate": selected.isoformat()},
                                "overview": overview, "availability": {"logs": can_read_logs, "nodes": is_admin},
                                "nodes": nodes,
                                "notes": ["日志统计基于已登记目录的原始分卷 bytes，受目录保留和发布时机影响，不代表实时物理 I/O。",
                                          "Coredump 统计基于选定上海日登记且当前可见的目录条目，不读取设备源文件正文。"]}
    if not can_read_logs:
        response.update({"daily": None, "topResources": None, "topResourcesTruncated": None, "coredumps": None})
        return response
    selected_lower, selected_upper, _ = _window(selected, 1)
    logs, coredumps = await asyncio.gather(
        _log_statistics(repo, tasks, resources, lower, upper, start_day, selected, days),
        _coredump_statistics(repo, resources, selected_lower, selected_upper),
    )
    response.update(logs)
    response["coredumps"] = coredumps
    return response


async def _cached_dashboard_payload(app: Any, user: dict[str, Any], **arguments: Any) -> dict[str, Any]:
    """短暂合并同窗口刷新；共享任务自身受时限和容量约束，不能长期占据缓存。"""
    is_admin = bool(user.get("isAdmin") or "*" in user.get("scopes", []) or "admin" in user.get("scopes", []))
    can_read_logs = "*" in user.get("scopes", []) or "logs:read" in user.get("scopes", [])
    key = (arguments["days"], arguments["selected"].isoformat(), can_read_logs, is_admin)
    cache = app.state.dashboard_cache
    stamp = monotonic()
    for cached_key, cached in list(cache.items()):
        if (
            cached.expires_at <= stamp
            or cached.task.cancelled()
            or (cached.task.done() and cached.task.exception() is not None)
        ):
            cache.pop(cached_key, None)
    entry = cache.get(key)
    if entry is not None:
        return await asyncio.shield(entry.task)
    if len(cache) >= _MAX_CACHE_ENTRIES:
        completed = [(cached_key, cached) for cached_key, cached in cache.items() if cached.task.done()]
        if completed:
            oldest_key, _ = min(completed, key=lambda item: item[1].expires_at)
            cache.pop(oldest_key, None)

    async def produce() -> dict[str, Any]:
        """共享任务自己超时，所有等待方取消后也不能遗留无限后台查询。"""
        async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
            return await _dashboard_payload(app.state.repo, user, **arguments)

    if len(cache) >= _MAX_CACHE_ENTRIES:
        return await produce()

    task = asyncio.create_task(produce())
    cache[key] = _CacheEntry(stamp + _REQUEST_TIMEOUT_SECONDS, task)

    def settle(completed: asyncio.Task) -> None:
        """失败和取消不缓存，成功快照只保留很短时间以限制轮询并发。"""
        entry = cache.get(key)
        if entry is None or entry.task is not completed:
            return
        if completed.cancelled() or completed.exception() is not None:
            cache.pop(key, None)
        else:
            cache[key] = _CacheEntry(monotonic() + _CACHE_SECONDS, completed)

    task.add_done_callback(settle)
    return await asyncio.shield(task)


async def shutdown_dashboard_cache(app: Any) -> None:
    """应用关闭前结束看板聚合任务，并释放仅用于短期刷新的响应缓存。"""
    cache = getattr(app.state, "dashboard_cache", None)
    if not cache:
        return
    tasks = {entry.task for entry in cache.values() if not entry.task.done()}
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    cache.clear()


def install_dashboard_routes(app: Any) -> None:
    """安装平台资源看板的只读路由，任务概览与日志统计采用不同权限可用性。"""
    app.state.dashboard_cache = {}

    @app.get("/api/v1/dashboard")
    async def dashboard(user: Annotated[dict, Depends(actor)],
                        days: Annotated[int, Query(description="统计窗口，只允许 7、14 或 30 个上海自然日")] = 7,
                        date: Annotated[CalendarDate | None, Query(description="统计截至的上海日期，格式 YYYY-MM-DD；默认今天")] = None):
        """返回截至选定上海日的资源概览及受权限保护的目录统计。"""
        authorize(user, "tasks:read")
        if days not in {7, 14, 30}:
            raise HTTPException(422, "days只允许7、14或30")
        selected = date or now().astimezone(SHANGHAI).date()
        today = now().astimezone(SHANGHAI).date()
        if selected > today:
            raise HTTPException(422, "date不能晚于上海今天")
        try:
            lower, upper, start_day = _window(selected, days)
        except OverflowError as exc:
            raise HTTPException(422, "date不能构成有效统计窗口") from exc
        try:
            return await _cached_dashboard_payload(app, user, days=days, selected=selected,
                                                   lower=lower, upper=upper, start_day=start_day)
        except TimeoutError as exc:
            raise HTTPException(503, "资源看板统计超时，请稍后重试") from exc
