"""鉴权后的任务索引覆盖状态；只返回元数据，不代理ES任意查询或正文。"""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query
from fastapi import Path as ApiPath

from camera_logs.common.api_time import parse_api_datetime
from camera_logs.common.database import now
from camera_logs.common.presentation_time import utc_iso
from camera_logs.common.security import actor, authorize
from camera_logs.indexing.query import query_evidence


async def coverage(repo, task_id, start, end):
    """按实际目录核实覆盖，每次最多10000项；截断时明确禁止声称完整覆盖。"""
    # 目录键固定UTC，使用既有(taskId,hour)索引；展示层转换不改变物理键。
    files = []
    truncated = False
    async for file in repo.db.files.find({"taskId": task_id, "status": {"$in": ['OPEN', 'READY']},
                                          "hour": {"$gte": utc_iso(start.replace(minute=0, second=0, microsecond=0)),
                                                   "$lt": utc_iso(end)}}):
        hour = datetime.fromisoformat(file['hour'])
        if hour < end and hour + timedelta(hours=1) > start:
            if len(files) == 10000:
                truncated = True
                break
            files.append(file)
    proof, status = await query_evidence(repo.settings, files)
    hours = {}
    for file in files:
        hour = datetime.fromisoformat(file['hour'])
        hours.setdefault(hour, []).append(file['id'] in proof)
    latest = max((hour for hour, flags in hours.items() if all(flags)), default=None)
    through = None
    if not truncated:
        for hour in sorted(hours):
            if not all(hours[hour]):
                break
            through = min(end, hour + timedelta(hours=1))
    return {"enabled": bool(repo.settings.elasticsearch_url), "status": status,
            "totalFiles": len(files), "coveredFiles": len(proof),
            "coverageComplete": status == 'AVAILABLE' and not truncated and len(files) == len(proof),
            "latestIndexedHour": latest, "contiguousCoveredThrough": through, "truncated": truncated,
            "fallback": 'RAW_SCAN'}


def install_index_routes(app):
    """按任务读取权限查询覆盖，索引服务凭据不返回给客户端。"""
    @app.get('/api/v1/tasks/{task_id}/search-index-status', description='查询指定时间范围的历史检索候选索引覆盖状态。索引只用于跳过不可能命中的分卷；coverageComplete=false或fallback=RAW_SCAN时，检索仍回退原始日志扫描。')
    async def index_status(task_id: Annotated[str, ApiPath(description='路径中指定要操作、读取或订阅的日志采集任务标识。')], user: Annotated[dict, Depends(actor)],
                           start: str | None = Query(default=None, description='查询范围开始时间，ISO 8601（不带时区按北京时间解释，也兼容显式时区） 且包含该时刻。'), end: str | None = Query(default=None, description='查询范围结束时间，ISO 8601（不带时区按北京时间解释，也兼容显式时区） 且不包含该时刻。')):
        """查询历史日志检索索引覆盖范围与原文回退状态。"""
        authorize(user, 'logs:read', task_id)
        repo = app.state.repo
        await repo.get('tasks', task_id)
        try:
            end_value = parse_api_datetime(end) if end else now()
            start_value = parse_api_datetime(start) if start else end_value - timedelta(days=1)
        except ValueError as error:
            raise HTTPException(422, 'start/end必须是有效 ISO 8601 日期时间') from error
        if start_value >= end_value or end_value - start_value > timedelta(days=366):
            raise HTTPException(422, 'start/end须为不超过366天的有效区间')
        return await coverage(repo, task_id, start_value, end_value)
