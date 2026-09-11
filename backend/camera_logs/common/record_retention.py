"""增长型 MongoDB 记录的保留策略。

该模块只负责把配置转换为安全、可解释的批量清理计划。调用方必须在维护窗口
显式执行计划；默认值 0 表示永久保留。活动中的命令、操作、租约和运行记录
永远不进入清理候选，防止影响任务恢复和预算一致性。
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
    terminal_statuses: frozenset[str] | None = None


RECORD_RETENTION_POLICIES: tuple[RecordRetentionPolicy, ...] = (
    RecordRetentionPolicy("authentication_records", "authentication_record_retention_days"),
    RecordRetentionPolicy("audit", "audit_record_retention_days"),
    RecordRetentionPolicy("events", "runtime_event_retention_days"),
    RecordRetentionPolicy(
        "commands", "command_history_retention_days", terminal_statuses=frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"})
    ),
    RecordRetentionPolicy(
        "operations", "operation_history_retention_days", time_field="completedAt",
        terminal_statuses=frozenset({"SUCCEEDED", "FAILED", "CANCELLED"}),
    ),
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

    返回值可直接传给 ``delete_many``，但不会在本模块内执行删除。活动命令和操作
    没有终态时不返回候选；认证/审计/运行事件则按时间字段清理，默认不返回条件。
    """
    cutoff = retention_cutoff(settings, policy, reference=reference)
    if cutoff is None:
        return None
    query: dict[str, Any] = {policy.time_field: {"$lt": cutoff}}
    if policy.terminal_statuses is not None:
        query["status"] = {"$in": sorted(policy.terminal_statuses)}
    return query


def build_retention_plan(settings: Any, *, reference: datetime | None = None) -> tuple[dict[str, Any], ...]:
    """返回当前配置下的非空清理计划，便于维护作业分批执行和审计。"""
    plan: list[dict[str, Any]] = []
    for policy in RECORD_RETENTION_POLICIES:
        query = cleanup_filter(settings, policy, reference=reference)
        if query is not None:
            plan.append({"collection": policy.collection, "query": query, "batchSize": 1000})
    return tuple(plan)
