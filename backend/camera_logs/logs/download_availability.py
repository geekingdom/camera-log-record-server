"""日志下载制品的可用性与保留快照。

下载作业记录需要长期保留以便审计和历史列表使用；可下载的物理制品则以
创建完成时的日志保留期为准。该模块集中判断，防止 API、浏览器票据和节点
读取在过期边界给出相互矛盾的结果。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from camera_logs.logs.maintenance import get_retention_days


def _utc(value: Any) -> datetime | None:
    """将新旧 BSON 时间统一为 UTC；异常历史值按不可下载处理。"""
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def download_available(job: dict[str, Any], timestamp: datetime | None = None) -> bool:
    """仅向成功、未到期且未进入清理的日志导出开放重复下载。

    新格式以 artifactCreatedAt 标识，必须带有发布与关闭写入证据。旧作业可
    缺少这些新字段，但仍必须保存已发布路径和绝对到期时间，避免把不完整的
    历史终态误报为可重复下载。
    """
    expires = _utc(job.get("expiresAt"))
    basic = (
        job.get("kind") in (None, "DOWNLOAD")
        and job.get("status") == "SUCCEEDED"
        and bool(job.get("resultPath"))
        and expires is not None
        and expires > (timestamp or datetime.now(UTC))
        and job.get("outputCleanupState") not in {"CLEANING", "CLEANED"}
    )
    if not basic:
        return False
    if job.get("artifactCreatedAt") is None:
        return True
    return bool(job.get("resultPath")) and job.get("outputExecutionState") == "CLOSED"


async def artifact_retention(repo: Any, created_at: datetime) -> dict[str, Any]:
    """制品完成时读取一次保留配置，后续设置变更不会追溯已有下载。"""
    days = await get_retention_days(repo)
    return {
        "artifactCreatedAt": created_at,
        "artifactRetentionDays": days,
        "expiresAt": created_at + timedelta(days=days),
    }
