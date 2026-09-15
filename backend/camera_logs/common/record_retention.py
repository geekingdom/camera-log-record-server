"""增长型 MongoDB 记录的保留策略。

该模块保留旧环境变量的预览计划，默认值 0 表示永久保留，不执行删除。
当前自动治理由 record_maintenance 和 record_archive 读取平台 recordRetention
配置执行；命令、操作、预算及运行的删除必须经过恢复引用与幂等保护检查，
不能将本模块的时间候选条件直接用于删除。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class RecordRetentionPolicy:
    """描述一个集合的安全保留期限和终态过滤条件。"""

    collection: str
    setting: str
    time_field: str = "createdAt"


RECORD_RETENTION_POLICIES: tuple[RecordRetentionPolicy, ...] = (
    RecordRetentionPolicy("authentication_records", "authentication_record_retention_days"),
    RecordRetentionPolicy("audit", "audit_record_retention_days"),
    RecordRetentionPolicy("events", "runtime_event_retention_days"),
)


def retention_cutoff(settings: Any, policy: RecordRetentionPolicy, *, reference: datetime | None = None) -> datetime | None:
    """返回 UTC 截止时间；未配置或为 0 时返回 None，代表永久保留。"""
    days = getattr(settings, policy.setting, 0)
    if not isinstance(days, int) or days <= 0:
        return None
    timestamp = reference or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC) - timedelta(days=days)


def cleanup_filter(settings: Any, policy: RecordRetentionPolicy, *, reference: datetime | None = None) -> dict[str, Any] | None:
    """构造只匹配过期且安全终态记录的查询条件。

    返回值仅供管理员预览或后续维护入口使用，本模块内绝不执行删除。认证、审计
    和运行事件按时间字段生成候选；认证新记录的实际自动到期仍由 ``expiresAt`` TTL
    索引执行。
    """
    cutoff = retention_cutoff(settings, policy, reference=reference)
    if cutoff is None:
        return None
    query: dict[str, Any] = {policy.time_field: {"$lt": cutoff}}
    return query


def build_retention_plan(settings: Any, *, reference: datetime | None = None) -> tuple[dict[str, Any], ...]:
    """返回当前配置下的非空清理计划，便于维护作业分批执行和审计。"""
    plan: list[dict[str, Any]] = []
    for policy in RECORD_RETENTION_POLICIES:
        query = cleanup_filter(settings, policy, reference=reference)
        if query is not None:
            plan.append({"collection": policy.collection, "query": query, "batchSize": 1000})
    return tuple(plan)
