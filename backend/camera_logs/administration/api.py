"""提供运行指标、审计查询和经人工确认的旧节点隔离接口。"""
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


def install_admin_routes(app):
    """安装仅管理员可访问的审计、指标和外部 fencing 路由。"""
    User = Annotated[dict, Depends(actor)]

    @app.get("/api/v1/audit-events")
    async def audit_events(request: Request, user: User, page: int = Query(1, ge=1), pageSize: int = Query(50, ge=1, le=100)):
        """按时间倒序分页读取审计事件，不暴露数据库内部字段。"""
        authorize(user, "admin")
        db = request.app.state.repo.db
        return {"items": [public(x) async for x in db.audit.find({}).sort("createdAt", -1).skip((page-1)*pageSize).limit(pageSize)],
                "total": await db.audit.count_documents({}), "page": page, "pageSize": pageSize}

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
