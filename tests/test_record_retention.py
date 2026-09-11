"""验证增长型 MongoDB 记录保留策略的默认永久和终态安全边界。"""

from datetime import UTC, datetime, timedelta

from camera_logs.common.config import Settings
from camera_logs.common.record_retention import (
    RECORD_RETENTION_POLICIES,
    build_retention_plan,
    cleanup_filter,
)


def test_record_retention_defaults_to_three_month_auth_history():
    settings = Settings(_env_file=None, encryption_key="", retention_days=7)
    plan = build_retention_plan(settings, reference=datetime.now(UTC))
    assert [item["collection"] for item in plan] == ["authentication_records"]


def test_record_retention_only_emits_explicit_policies_and_terminal_statuses():
    reference = datetime(2026, 9, 11, tzinfo=UTC)
    settings = Settings(_env_file=None, encryption_key="", audit_record_retention_days=30,
                        command_history_retention_days=14, operation_history_retention_days=7)
    plan = {item["collection"]: item for item in build_retention_plan(settings, reference=reference)}
    assert set(plan) == {"authentication_records", "audit", "commands", "operations"}
    assert plan["audit"]["query"] == {"createdAt": {"$lt": reference - timedelta(days=30)}}
    assert plan["commands"]["query"]["status"]["$in"] == ["CANCELLED", "FAILED", "SUCCEEDED", "UNKNOWN"]
    assert plan["operations"]["query"]["completedAt"]["$lt"] == reference - timedelta(days=7)


def test_invalid_or_zero_retention_is_not_a_cleanup_query():
    settings = Settings(_env_file=None, encryption_key="", runtime_event_retention_days=0)
    policy = next(item for item in RECORD_RETENTION_POLICIES if item.collection == "events")
    assert cleanup_filter(settings, policy) is None
