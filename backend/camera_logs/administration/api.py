"""提供运行指标、受限审计查询和经人工确认的旧节点隔离接口。"""
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from pydantic import BaseModel, Field

from camera_logs.common.database import now, public
from camera_logs.common.security import actor, authorize


class FenceConfirmation(BaseModel):
    """要求管理员提交固定确认词和基础设施隔离证据。"""
    confirmation: str
    evidence: str = Field(min_length=10, max_length=2000)


def _utc_range(start: str | None, end: str | None) -> dict[str, datetime]:
    """解析管理查询时间范围，拒绝无时区、反向和过宽条件以控制扫描范围。"""
    if start is None and end is None:
        return {}
    if start is None or end is None:
        raise HTTPException(422, "开始和结束时间必须同时提供")
    try:
        lower, upper = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError as exc:
        raise HTTPException(422, "时间必须使用带时区的 ISO 8601 格式") from exc
    if lower.tzinfo is None or upper.tzinfo is None:
        raise HTTPException(422, "时间必须显式包含 UTC 时区或偏移")
    lower, upper = lower.astimezone(UTC), upper.astimezone(UTC)
    if upper <= lower or upper - lower > timedelta(days=31):
        raise HTTPException(422, "时间范围必须大于零且不超过31天")
    return {"$gte": lower, "$lt": upper}


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
    cursor = db.events.aggregate([
        {"$match": query},
        {"$addFields": {"_eventTime": {"$ifNull": ["$createdAt", "$detectedAt"]}}},
        {"$sort": {"_eventTime": -1}},
        {"$skip": (page - 1) * page_size},
        {"$limit": page_size},
        {"$project": {"_eventTime": 0}},
    ])
    items = []
    async for item in cursor:
        item = public(item)
        if item.get("createdAt") is None and item.get("detectedAt") is not None:
            item["createdAt"] = item["detectedAt"]
        items.append(item)
    return {
        "items": items,
        "total": await db.events.count_documents(query),
        "page": page,
        "pageSize": page_size,
    }


def install_admin_routes(app):
    """安装仅管理员可访问的审计、指标和外部 fencing 路由。"""
    User = Annotated[dict, Depends(actor)]

    @app.get("/api/v1/audit-events")
    async def audit_events(
        request: Request,
        user: User,
        page: int = Query(1, ge=1),
        pageSize: int = Query(50, ge=1, le=100),
        action: str | None = Query(default=None, max_length=128),
        actor_id: str | None = Query(default=None, alias="actor", max_length=128),
        task_id: str | None = Query(default=None, alias="taskId", max_length=128),
        start: str | None = Query(default=None, max_length=64),
        end: str | None = Query(default=None, max_length=64),
    ):
        """按操作、主体、目标任务和受限 UTC 范围查询审计事件。"""
        authorize(user, "admin")
        db = request.app.state.repo.db
        query = {key: value for key, value in {
            "action": action, "actor": actor_id, "targetId": task_id,
        }.items() if value}
        if stamp := _utc_range(start, end):
            query["createdAt"] = stamp
        return await _event_page(db, "audit", query, page, pageSize)

    @app.get("/api/v1/runtime-events")
    async def runtime_events(
        request: Request,
        user: User,
        page: int = Query(1, ge=1),
        pageSize: int = Query(50, ge=1, le=100),
        task_id: str | None = Query(default=None, alias="taskId", max_length=128),
        node_id: str | None = Query(default=None, alias="nodeId", max_length=128),
        event_type: str | None = Query(default=None, alias="type", max_length=128),
        start: str | None = Query(default=None, max_length=64),
        end: str | None = Query(default=None, max_length=64),
    ):
        """按任务、节点、类别和受限 UTC 范围查询不含日志正文的运行事件。"""
        authorize(user, "admin")
        db = request.app.state.repo.db
        query = {key: value for key, value in {
            "taskId": task_id, "nodeId": node_id, "type": event_type,
        }.items() if value}
        if stamp := _utc_range(start, end):
            query["$or"] = [
                {"createdAt": stamp},
                {"createdAt": {"$exists": False}, "detectedAt": stamp},
            ]
        return await _runtime_event_page(db, query, page, pageSize)

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
    async def confirm_isolation(node_id: str, body: FenceConfirmation, request: Request, user: User):
        """确认旧节点已在基础设施层隔离后，才释放其被阻塞任务的调度锁。"""
        authorize(user, "admin")
        if body.confirmation != "CONFIRM_NODE_ISOLATED":
            raise HTTPException(422, "必须先在基础设施层停止或隔离旧节点，并提供确认及操作依据")
        repo = request.app.state.repo
        node = await repo.get("nodes", node_id)
        if (now()-node["heartbeat"].replace(tzinfo=now().tzinfo)).total_seconds() < 30:
            raise HTTPException(409, "节点仍在发送心跳，不能确认隔离")
        await repo.audit(user["id"], "confirm_node_isolation", node_id)
        await repo.db.events.insert_one({"nodeId": node_id, "type": "EXTERNAL_FENCING_CONFIRMED",
            "actor": user["id"], "evidence": body.evidence, "createdAt": now()})
        await repo.db.nodes.update_one({"id": node_id}, {"$set": {"accepting": False, "isolated": True}})
        async for task in repo.db.tasks.find({"nodeId": node_id, "status": "BLOCKED"}):
            await repo.db.endpoint_locks.delete_one({"taskId": task["id"], "runId": task.get("runId")})
            await repo.db.tasks.update_one({"id": task["id"], "nodeId": node_id, "status": "BLOCKED"},
                {"$set": {"nodeId": None, "status": "PENDING" if task["desiredState"] == "RUNNING" else "STOPPED"}})
        return {"nodeId": node_id, "status": "ISOLATED"}
