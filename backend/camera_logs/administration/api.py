"""提供运行指标、受限审计查询和经人工确认的旧节点隔离接口。"""
import json
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query, Request
from fastapi import Path as ApiPath
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from pydantic import BaseModel, Field

from camera_logs.administration.event_presenter import audit_event_summary_clause
from camera_logs.administration.event_queries import (
    event_cursor_page,
    event_page,
    runtime_event_cursor_page,
    runtime_event_filters,
    runtime_event_page,
)
from camera_logs.administration.isolation import confirm_node_isolation
from camera_logs.common.api_time import parse_api_datetime
from camera_logs.common.database import public
from camera_logs.common.observability import redact
from camera_logs.common.security import actor, authorize


class FenceConfirmation(BaseModel):
    """要求管理员提交固定确认词和基础设施隔离证据。"""
    confirmation: str = Field(description='须填写固定确认词 CONFIRM_NODE_ISOLATED，并先确认节点旧实例已经在基础设施侧隔离；此接口不替调用方执行物理隔离。')
    evidence: str = Field(min_length=10, max_length=2000, description='确认旧实例隔离所依据的工单、操作记录或其他可核验证据。')


EventLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
EventOutcome = Literal["PENDING", "SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"]


def _utc_range(start: str | None, end: str | None) -> dict[str, datetime]:
    """解析管理查询时间范围；无时区输入按上海墙钟时间解释。"""
    if start is None and end is None:
        return {}
    if start is None or end is None:
        raise HTTPException(422, "开始和结束时间必须同时提供")
    try:
        lower, upper = parse_api_datetime(start), parse_api_datetime(end)
    except ValueError as exc:
        raise HTTPException(422, "时间必须使用有效 ISO 8601 日期时间格式") from exc
    if upper <= lower or upper - lower > timedelta(days=31):
        raise HTTPException(422, "时间范围必须大于零且不超过31天")
    return {"$gte": lower, "$lt": upper}


def _redacted_evidence(value: str) -> str:
    """递归净化 JSON 格式隔离依据；普通文本仍按统一日志规则脱敏。"""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return str(redact(value))
    return json.dumps(redact(parsed), ensure_ascii=False, separators=(",", ":"))


async def _event_page(db, collection: str, query: dict, page: int, page_size: int) -> dict:
    """按确定的创建时间倒序返回公开事件字段，计数与页面使用完全相同的条件。"""
    cursor = db[collection].find(query).sort("createdAt", -1).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [public(item) async for item in cursor],
        "total": await db[collection].count_documents(query),
        "page": page,
        "pageSize": page_size,
    }


async def _runtime_event_page(db, query: dict, page: int, page_size: int) -> dict:
    """兼容历史 detectedAt 事件，统一按实际发生时间排序并补出展示时间。"""
    # 保留此低层兼容入口给驱动接口测试；路由使用下方含展示推导筛选的查询器。
    cursor = await db.events.aggregate([
        {"$match": query}, {"$addFields": {"_eventTime": {"$ifNull": ["$createdAt", "$detectedAt"]}}},
        {"$sort": {"_eventTime": -1}}, {"$skip": (page - 1) * page_size},
        {"$limit": page_size}, {"$project": {"_eventTime": 0}},
    ])
    items = []
    async for item in cursor:
        item = public(item)
        if item.get("createdAt") is None and item.get("detectedAt") is not None:
            item["createdAt"] = item["detectedAt"]
        items.append(item)
    return {"items": items, "total": await db.events.count_documents(query), "page": page, "pageSize": page_size}


def install_admin_routes(app):
    """安装仅管理员可访问的审计、指标和外部 fencing 路由。"""
    User = Annotated[dict, Depends(actor)]

    @app.get("/api/v1/audit-events")
    async def audit_events(
        request: Request,
        user: User,
        page: int = Query(1, ge=1, description='从 1 开始的分页页码。'),
        pageSize: int = Query(50, ge=1, le=100, description='每页返回的记录数，受接口允许的最大值限制。'),
        cursor: str | None = Query(default=None, max_length=4096, description='上页返回的稳定分页游标；管理事件传空字符串开启首屏游标查询，续页使用nextCursor并保持原筛选条件。不能与大于1的page同用。'),
        includeTotal: bool = Query(default=False, description='管理事件游标查询是否额外统计精确总数，默认false并返回total:null；统计会增加数据库扫描成本。'),
        action: str | None = Query(default=None, max_length=128, description='按审计操作类型筛选，例如 create_task 或 control:PAUSED。'), level: Annotated[EventLevel | None, Query(description='按事件级别筛选：DEBUG 调试、INFO 信息、WARNING 警告、ERROR 错误。')] = None,
        summary: str | None = Query(default=None, max_length=128, description='按审计或运行事件的中文摘要字面子串筛选，也兼容输入内部操作或事件代码。'),
        outcome: Annotated[EventOutcome | None, Query(description='按处理结果筛选：PENDING 待处理、SUCCEEDED 成功、FAILED 失败、CANCELLED 取消、UNKNOWN 结果未知。')] = None, request_id: str | None = Query(default=None, alias="requestId", max_length=128, description='按平台分配的请求追踪编号筛选审计或请求记录。'),
        actor_id: str | None = Query(default=None, alias="actor", max_length=128, description='按执行该审计操作的平台用户标识筛选。'),
        task_id: str | None = Query(default=None, alias="taskId", max_length=128, description='只查询关联此采集任务 ID 的事件，ID 由任务列表返回。'),
        start: str | None = Query(default=None, max_length=64, description='事件时间下界，包含该时刻；如 2026-10-19T12:00:00，无时区按北京时间解释。须与 end 同时提供，范围大于零且不超过 31 天。'),
        end: str | None = Query(default=None, max_length=64, description='事件时间上界，不包含该时刻；如 2026-10-20T12:00:00，无时区按北京时间解释。须与 start 同时提供，范围大于零且不超过 31 天。'),
    ):
        """按操作、主体、目标任务和受限 UTC 范围查询审计事件。"""
        authorize(user, "admin")
        db = request.app.state.repo.db
        query = {key: value for key, value in {
            "action": action, "actor": actor_id, "targetId": task_id, "level": level, "outcome": outcome, "requestId": request_id,
        }.items() if value}
        if summary:
            query = {"$and": [query, audit_event_summary_clause(summary)]}
        if stamp := _utc_range(start, end):
            query["createdAt"] = stamp
        if cursor is not None:
            if page != 1:
                raise HTTPException(422, "游标分页不能与大于1的页码同时使用")
            return await event_cursor_page(db, "audit", query, cursor, pageSize, include_total=includeTotal)
        return await event_page(db, "audit", query, page, pageSize)

    @app.get("/api/v1/runtime-events")
    async def runtime_events(
        request: Request,
        user: User,
        page: int = Query(1, ge=1, description='从 1 开始的分页页码。'),
        pageSize: int = Query(50, ge=1, le=100, description='每页返回的记录数，受接口允许的最大值限制。'),
        cursor: str | None = Query(default=None, max_length=4096, description='上页返回的稳定分页游标；管理事件传空字符串开启首屏游标查询，续页使用nextCursor并保持原筛选条件。不能与大于1的page同用。'),
        includeTotal: bool = Query(default=False, description='管理事件游标查询是否额外统计精确总数，默认false并返回total:null；统计会增加数据库扫描成本。'),
        task_id: str | None = Query(default=None, alias="taskId", max_length=128, description='只查询关联此采集任务 ID 的事件，ID 由任务列表返回。'),
        node_id: str | None = Query(default=None, alias="nodeId", max_length=128, description='按 Worker 节点标识筛选运行事件。'),
        event_type: str | None = Query(default=None, alias="type", max_length=128, description='按运行事件内部类型精确筛选，例如 CONNECTION_GAP；页面优先使用summary。'), level: Annotated[EventLevel | None, Query(description='按事件级别筛选：DEBUG 调试、INFO 信息、WARNING 警告、ERROR 错误。')] = None,
        summary: str | None = Query(default=None, max_length=128, description='按审计或运行事件的中文摘要字面子串筛选，也兼容输入内部操作或事件代码。'),
        object_value: str | None = Query(default=None, alias="object", max_length=128, description='按运行事件对象的资源、任务、节点或核心转储文件名称、IP 或标识进行字面子串筛选。'),
        outcome: Annotated[EventOutcome | None, Query(description='按处理结果筛选：PENDING 待处理、SUCCEEDED 成功、FAILED 失败、CANCELLED 取消、UNKNOWN 结果未知。')] = None, request_id: str | None = Query(default=None, alias="requestId", max_length=128, description='按平台分配的请求追踪编号筛选审计或请求记录。'),
        start: str | None = Query(default=None, max_length=64, description='事件时间下界，包含该时刻；如 2026-10-19T12:00:00，无时区按北京时间解释。须与 end 同时提供，范围大于零且不超过 31 天。'),
        end: str | None = Query(default=None, max_length=64, description='事件时间上界，不包含该时刻；如 2026-10-20T12:00:00，无时区按北京时间解释。须与 start 同时提供，范围大于零且不超过 31 天。'),
    ):
        """按任务、节点、摘要、对象和受限 UTC 范围查询不含日志正文的运行事件。"""
        authorize(user, "admin")
        db = request.app.state.repo.db
        query = {key: value for key, value in {
            "taskId": task_id, "nodeId": node_id, "type": event_type, "level": level, "outcome": outcome, "requestId": request_id,
        }.items() if value}
        query = await runtime_event_filters(db, query, summary=summary, object_value=object_value)
        time_range = _utc_range(start, end)
        if cursor is not None:
            if page != 1:
                raise HTTPException(422, "游标分页不能与大于1的页码同时使用")
            return await runtime_event_cursor_page(
                db, query, cursor, pageSize, time_range=time_range, include_total=includeTotal,
            )
        return await runtime_event_page(db, query, page, pageSize, time_range=time_range)

    @app.get("/api/v1/request-events")
    async def request_events(request: Request, user: User, page: int = Query(1, ge=1, description='从 1 开始的分页页码。'), pageSize: int = Query(50, ge=1, le=100, description='每页返回的记录数，受接口允许的最大值限制。'),
                             cursor: str | None = Query(default=None, max_length=4096, description='上页返回的稳定分页游标；管理事件传空字符串开启首屏游标查询，续页使用nextCursor并保持原筛选条件。不能与大于1的page同用。'), includeTotal: bool = Query(default=False, description='管理事件游标查询是否额外统计精确总数，默认false并返回total:null；统计会增加数据库扫描成本。'),
                             method: str | None = Query(default=None, max_length=16, description='按 HTTP 请求方法筛选请求记录。'), route: str | None = Query(default=None, max_length=256, description='按 API 路由模板筛选请求记录。'),
                             status: int | None = Query(default=None, ge=100, le=599, description='按 HTTP 响应状态码筛选请求记录。'), level: Annotated[EventLevel | None, Query(description='按事件级别筛选：DEBUG 调试、INFO 信息、WARNING 警告、ERROR 错误。')] = None,
                             outcome: Annotated[EventOutcome | None, Query(description='按处理结果筛选：PENDING 待处理、SUCCEEDED 成功、FAILED 失败、CANCELLED 取消、UNKNOWN 结果未知。')] = None, request_id: str | None = Query(default=None, alias="requestId", max_length=128, description='按平台分配的请求追踪编号筛选审计或请求记录。'),
                             client_ip: str | None = Query(default=None, alias="clientIp", max_length=64, description='按实际访问平台的调用方来源 IP 筛选请求记录。'), task_id: str | None = Query(default=None, alias="taskId", max_length=128, description='只查询关联此采集任务 ID 的事件，ID 由任务列表返回。'),
                             start: str | None = Query(default=None, max_length=64, description='事件时间下界，包含该时刻；如 2026-10-19T12:00:00，无时区按北京时间解释。须与 end 同时提供，范围大于零且不超过 31 天。'), end: str | None = Query(default=None, max_length=64, description='事件时间上界，不包含该时刻；如 2026-10-20T12:00:00，无时区按北京时间解释。须与 start 同时提供，范围大于零且不超过 31 天。')):
        """查询失败或写请求的无正文访问事件，供管理员关联 API 排障信息。"""
        authorize(user, "admin")
        query = {key: value for key, value in {"method": method, "route": route, "httpStatus": status,
                 "level": level, "outcome": outcome, "requestId": request_id, "clientIp": client_ip, "taskId": task_id}.items() if value is not None}
        if stamp := _utc_range(start, end): query["createdAt"] = stamp
        if cursor is not None:
            if page != 1:
                raise HTTPException(422, "游标分页不能与大于1的页码同时使用")
            return await event_cursor_page(
                request.app.state.repo.db, "request_events", query, cursor, pageSize, include_total=includeTotal,
            )
        return await event_page(request.app.state.repo.db, "request_events", query, page, pageSize)

    @app.get("/metrics")
    async def metrics(request: Request, user: User):
        """临时聚合任务和节点状态，输出管理员专用 Prometheus 指标。"""
        authorize(user, "admin")
        registry = CollectorRegistry()
        count = Gauge("camera_logs_tasks", "Tasks by observed state", ["state"], registry=registry)
        db = request.app.state.repo.db
        async for item in await db.tasks.aggregate([{"$group": {"_id": "$status", "count": {"$sum": 1}}}]):
            count.labels(item["_id"]).set(item["count"])
        disk = Gauge("camera_logs_disk_percent", "Node log filesystem used percent", ["node"], registry=registry)
        rate = Gauge("camera_logs_input_bytes_per_second", "Node observed input byte rate", ["node"], registry=registry)
        async for node in db.nodes.find({}):
            disk.labels(node["id"]).set(node.get("diskPercent", 0))
            rate.labels(node["id"]).set(node.get("inputBytesPerSecond", 0))
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/api/v1/nodes/{node_id}/confirm-isolation")
    async def confirm_isolation(node_id: Annotated[str, ApiPath(description='需要确认旧实例已完成基础设施隔离的 Worker 节点 ID，由节点列表返回。')], body: FenceConfirmation, request: Request, user: User):
        """确认旧节点已在基础设施层隔离后，才释放其被阻塞任务的调度锁。"""
        authorize(user, "admin")
        if body.confirmation != "CONFIRM_NODE_ISOLATED":
            raise HTTPException(422, "必须先在基础设施层停止或隔离旧节点，并提供确认及操作依据")
        return await confirm_node_isolation(request.app.state.repo, node_id, user["id"],
                                            _redacted_evidence(body.evidence))
