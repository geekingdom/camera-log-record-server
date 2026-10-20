"""提供资源 CPU/内存趋势的有界历史接口，不暴露命令响应和管理员规则正文。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from fastapi import Path as ApiPath

from camera_logs.common.api_time import parse_api_datetime
from camera_logs.common.security import actor, authorize
from camera_logs.resource_metrics.models import parse_monitor_config
from camera_logs.resource_metrics.store import ResourceMetricStore


def _time(value: str | None, fallback: datetime) -> datetime:
    """接受 ISO 时间；无时区输入按上海墙钟时间解释为 UTC 绝对时刻。"""
    if value is None:
        return fallback
    try:
        return parse_api_datetime(value)
    except ValueError as error:
        raise HTTPException(422, "时间必须为有效 ISO 8601 日期时间") from error


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
        resource_id: Annotated[str, ApiPath(description='路径中指定要查询的设备资源标识。')],
        request: Request,
        user: Annotated[dict, Depends(actor)],
        start: Annotated[str | None, Query(description='查询范围开始时间，ISO 8601（不带时区按北京时间解释，也兼容显式时区） 且包含该时刻。')] = None,
        end: Annotated[str | None, Query(description='查询范围结束时间，ISO 8601（不带时区按北京时间解释，也兼容显式时区） 且不包含该时刻。')] = None,
        limit: int = Query(200, ge=1, le=2000, description='本页最多返回的采样记录数（不是字节数），1 至 2000，默认 200；使用 nextCursor 继续读取。'),
        cursor: str | None = Query(default=None, max_length=256, description='上页返回的稳定分页游标；管理事件传空字符串开启首屏游标查询，续页使用nextCursor并保持原筛选条件。不能与大于1的page同用。'),
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
