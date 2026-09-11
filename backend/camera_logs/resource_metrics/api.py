"""提供资源 CPU/内存趋势的有界历史接口，不暴露命令响应和管理员规则正文。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request

from camera_logs.common.security import actor, authorize
from camera_logs.resource_metrics.models import parse_monitor_config
from camera_logs.resource_metrics.store import ResourceMetricStore


def _time(value: str | None, fallback: datetime) -> datetime:
    """只接受带时区 ISO 时间，避免浏览器本地时区改变历史范围。"""
    if value is None:
        return fallback
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(422, "时间必须为带时区的 ISO 8601 格式") from error
    if parsed.tzinfo is None:
        raise HTTPException(422, "时间必须包含时区")
    return parsed.astimezone(UTC)


def _public_config(config: dict, version: int) -> dict:
    """趋势页面只需指标标签和单位，不应向所有只读用户公开设备命令和正则。"""
    return {
        "version": version,
        "intervalSeconds": config["intervalSeconds"],
        "retentionDays": config["retentionDays"],
        "items": [{key: item[key] for key in ("id", "name", "unit", "enabled")} for item in config["items"]],
    }


def install_resource_metric_routes(app) -> None:
    """注册资源指标历史，权限沿用资源页面实际使用的 tasks:read。"""

    @app.get("/api/v1/resources/{resource_id}/resource-metrics")
    async def resource_metrics(
        resource_id: str,
        request: Request,
        user: Annotated[dict, Depends(actor)],
        start: str | None = None,
        end: str | None = None,
        limit: int = Query(200, ge=1, le=2000),
        cursor: str | None = Query(default=None, max_length=256),
    ):
        """按 UTC 时间范围读取最多31天资源指标；游标避免深页扫描与精确总数。"""
        authorize(user, "tasks:read")
        database = request.app.state.repo.db
        resource = await database.resources.find_one({"id": resource_id})
        if resource is None:
            raise HTTPException(404, "设备资源不存在")
        upper = _time(end, datetime.now(UTC))
        lower = _time(start, upper - timedelta(hours=1))
        if lower >= upper or upper - lower > timedelta(days=31):
            raise HTTPException(422, "时间范围必须大于零且不超过31天")
        settings = await database.platform_settings.find_one(
            {"id": "platform"}, {"resourceMonitor": 1, "version": 1}
        )
        config = parse_monitor_config(settings.get("resourceMonitor") if settings else None)
        try:
            result = await ResourceMetricStore(database).history(
                resource_id, lower, upper, limit=limit, cursor=cursor
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        owner = None
        owner_id = resource.get("resourceMonitorLeaseTaskId")
        lease = resource.get("resourceMonitorLeaseUntil")
        if owner_id and lease and lease.replace(tzinfo=UTC) > datetime.now(UTC):
            task = await database.tasks.find_one(
                {
                    "id": owner_id,
                    "status": "COLLECTING",
                    "desiredState": "RUNNING",
                    "runId": resource.get("resourceMonitorLeaseRunId"),
                    "generation": resource.get("resourceMonitorLeaseGeneration"),
                    "nodeId": resource.get("resourceMonitorLeaseNodeId"),
                },
                {"id": 1, "name": 1},
            )
            if task:
                owner = {"id": task["id"], "name": task.get("name", task["id"])}
        return result | {
            "config": _public_config(config, int(settings.get("version", 1)) if settings else 1),
            "owner": owner,
        }
