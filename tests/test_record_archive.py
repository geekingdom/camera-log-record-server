"""增长型记录归档维护的恢复保护与重复执行测试。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from camera_logs.common import record_archive, record_purge
from mongomock_motor import AsyncMongoMockClient


@pytest.fixture
def repository(monkeypatch):
    """使用随机内存库替代真实副本集事务；生产事务由独立部署验证覆盖。"""
    repo = SimpleNamespace(db=AsyncMongoMockClient().record_archive)

    async def transaction(_repo, callback):
        return await callback(None)

    monkeypatch.setattr(record_archive, "_transaction", transaction)
    monkeypatch.setattr(record_purge, "_transaction", transaction)
    return repo


async def _run(repo, identifier="old", *, task="task", ended=None):
    ended = ended or datetime(2026, 1, 1, tzinfo=UTC)
    await repo.db.runs.insert_one({"id": identifier, "taskId": task, "startedAt": ended - timedelta(hours=1), "endedAt": ended})
    return ended


async def test_completed_run_archives_bounded_summary_and_is_idempotent(repository):
    """已结算结束运行先产生无命令正文摘要，再删除预算、命令和运行本体。"""
    ended = await _run(repository)
    await repository.db.commands.insert_one({"id": "command", "taskId": "task", "runId": "old", "status": "SENT", "command": "secret"})
    await repository.db.budgets.insert_one({"_id": "old:periodic", "attempts": 2})
    result = await record_archive.maintain_growth_records(repository, {"runDays": 30}, reference=ended + timedelta(days=31))

    assert result["archivedRuns"] == 1
    archive = await repository.db.run_archives.find_one({"runId": "old"})
    assert archive["commandCount"] == 1 and archive["budgetCount"] == 1 and "command" not in archive
    assert await repository.db.runs.find_one({"id": "old"}) is None
    assert await repository.db.commands.find_one({"id": "command"}) is None
    assert (await record_archive.maintain_growth_records(repository, {"runDays": 30}, reference=ended + timedelta(days=31)))["archivedRuns"] == 0
    assert await repository.db.run_archives.count_documents({"runId": "old"}) == 1


@pytest.mark.parametrize("guard", ["UNKNOWN", "task", "idempotency"])
async def test_run_with_recovery_reference_is_never_archived(repository, guard):
    """未知发送、当前任务引用和有效幂等映射均阻止整组删除。"""
    ended = await _run(repository)
    if guard == "UNKNOWN":
        await repository.db.commands.insert_one({"id": "command", "taskId": "task", "runId": "old", "status": "UNKNOWN"})
    elif guard == "task":
        await repository.db.tasks.insert_one({"id": "task", "runId": "old", "nodeId": None})
    else:
        await repository.db.commands.insert_one({"id": "command", "taskId": "task", "runId": "old", "status": "SENT"})
        await repository.db.idempotency.insert_one({"resourceId": "command", "expiresAt": datetime.now(UTC) + timedelta(days=1)})
    result = await record_archive.maintain_growth_records(repository, {"runDays": 30}, reference=ended + timedelta(days=31))
    assert result["archivedRuns"] == 0 and result["protected"] == 1
    assert await repository.db.runs.find_one({"id": "old"})


async def test_event_and_audit_cleanup_are_limited_and_active_event_is_preserved(repository):
    """审计和事件按固定批量清理，关联活动任务的运行事件不能因到期删除。"""
    reference = datetime(2026, 3, 1, tzinfo=UTC)
    for index in range(3):
        await repository.db.audit.insert_one({"createdAt": reference - timedelta(days=100), "index": index})
    await repository.db.tasks.insert_one({"id": "active", "nodeId": "node"})
    await repository.db.events.insert_many([
        {"createdAt": reference - timedelta(days=100), "taskId": "active"},
        {"createdAt": reference - timedelta(days=100), "taskId": "ended"},
    ])
    result = await record_archive.maintain_growth_records(repository, {"auditDays": 90, "eventDays": 90}, reference=reference, batch_size=2)
    assert result["audit"] == 2 and result["events"] == 1
    assert await repository.db.audit.count_documents({}) == 1
    assert await repository.db.events.count_documents({"taskId": "active"}) == 1


async def test_every_command_idempotency_unknown_status_regex_and_archived_events_are_safe(repository):
    """后续命令映射、未知状态和特殊运行ID受保护；归档运行事件可按期限删除。"""
    ended = await _run(repository, "old.+")
    await repository.db.commands.insert_many([
        {"id": "first", "taskId": "task", "runId": "old.+", "status": "SENT"},
        {"id": "later", "taskId": "task", "runId": "old.+", "status": "SENT"},
        {"id": "unknown", "taskId": "other", "runId": "other", "status": "NEW_STATE"},
    ])
    await repository.db.budgets.insert_many([{"_id": "old.+:one"}, {"_id": "oldx:must-stay"}])
    await repository.db.idempotency.insert_one({"resourceId": "later", "expiresAt": datetime.now(UTC) + timedelta(days=1)})
    await repository.db.events.insert_one({"createdAt": ended, "runId": "archived"})
    result = await record_archive.maintain_growth_records(repository, {"runDays": 30, "eventDays": 30}, reference=ended + timedelta(days=31))
    assert result["archivedRuns"] == 0
    assert await repository.db.budgets.find_one({"_id": "old.+:one"})
    assert await repository.db.budgets.find_one({"_id": "oldx:must-stay"})
    assert await repository.db.events.find_one({"runId": "archived"}) is None


async def test_protected_old_events_do_not_starve_later_deletable_event(repository):
    """扫描窗口超过删除批量，前序受保护记录不会永久挡住后续候选。"""
    reference = datetime(2026, 3, 1, tzinfo=UTC)
    await repository.db.tasks.insert_one({"id": "active", "nodeId": "node"})
    await repository.db.events.insert_many([
        {"createdAt": reference - timedelta(days=100), "taskId": "active", "index": index} for index in range(10)
    ] + [{"createdAt": reference - timedelta(days=99), "taskId": "ended", "index": "delete"}])
    removed = 0
    for _ in range(12):
        result = await record_archive.maintain_growth_records(repository, {"eventDays": 90}, reference=reference, batch_size=1)
        removed += result["events"]
    assert removed == 1
    assert await repository.db.events.find_one({"index": "delete"}) is None


async def test_large_run_archives_in_bounded_chunks_without_double_count(repository):
    """超过1000条命令的运行按100条分段，多轮摘要累计恰好一次。"""
    ended = await _run(repository)
    await repository.db.commands.insert_many([
        {"id": f"command-{index}", "taskId": "task", "runId": "old", "status": "SENT"} for index in range(1001)
    ])
    result = await record_archive.maintain_growth_records(repository, {"runDays": 30}, reference=ended + timedelta(days=31))
    assert result["archivedRuns"] == 0 and result["partialRuns"] == 1
    assert await repository.db.commands.count_documents({"runId": "old"}) == 901
    assert await repository.db.runs.find_one({"id": "old"})
    for _ in range(10):
        await record_archive.maintain_growth_records(repository, {"runDays": 30}, reference=ended + timedelta(days=31))
    summary = await repository.db.run_archives.find_one({"runId": "old"})
    assert summary["state"] == "ARCHIVED" and summary["commandCount"] == 1001
    assert summary["commandStatuses"] == {"SENT": 1001}
    assert not await repository.db.runs.find_one({"id": "old"})


async def test_duplicate_idempotency_mappings_only_pin_their_command(repository):
    """同命令的重复映射不能阻止同批其它已结束命令归档。"""
    ended = await _run(repository)
    await repository.db.commands.insert_many([
        {"id": "pinned", "taskId": "task", "runId": "old", "status": "SENT"},
        {"id": "free", "taskId": "task", "runId": "old", "status": "SENT"}])
    await repository.db.idempotency.insert_many([{"resourceId": "pinned", "expiresAt": None} for _ in range(101)])
    await record_archive.maintain_growth_records(repository, {"runDays": 30}, reference=ended+timedelta(days=31))
    assert await repository.db.commands.find_one({"id": "pinned"})
    assert not await repository.db.commands.find_one({"id": "free"})
    assert (await repository.db.run_archives.find_one({"runId": "old"}))["commandCount"] == 1


async def test_null_maintenance_lease_is_recoverable(repository):
    """旧空租约不阻塞维护，普通未来租约仍然拒绝接管。"""
    await repository.db.record_archive_maintenance.insert_one({"_id": "growth-records", "expiresAt": None})
    assert (await record_archive.maintain_growth_records(repository, {}))["leased"]
