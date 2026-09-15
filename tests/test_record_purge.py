"""过期记录清理的事务围栏和引用保护测试。"""
import gzip
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from bson import json_util
from camera_logs.common import record_purge
from mongomock_motor import AsyncMongoMockClient


@pytest.fixture
def repo(monkeypatch):
    value=SimpleNamespace(db=AsyncMongoMockClient().purge)
    async def transaction(_repo, callback): return await callback(None)
    monkeypatch.setattr(record_purge,"_transaction",transaction)
    return value

async def test_operations_protects_current_and_idempotent_then_counts_deleted(repo):
    old=datetime.now(UTC)-timedelta(days=100)
    await repo.db.record_archive_maintenance.insert_one({"_id":"growth-records","owner":"owner","expiresAt":datetime.now(UTC)+timedelta(minutes=1)})
    await repo.db.tasks.insert_one({"id":"task","controlOperationId":"current"})
    await repo.db.operations.insert_many([{"id":"current","taskId":"task","status":"SUCCEEDED","createdAt":old},{"id":"idem","taskId":"other","status":"FAILED","createdAt":old},{"id":"delete","taskId":"other","status":"CANCELLED","createdAt":old}])
    await repo.db.idempotency.insert_one({"resourceId":"idem","expiresAt":datetime.now(UTC)+timedelta(days=1)})
    result=await record_purge.purge_expired_records(repo,"operations",datetime.now(UTC)-timedelta(days=90),"owner")
    assert result["removed"]==1 and result["protected"]==2
    assert await repo.db.record_purge_days.find_one({"collection":"operations"})

async def test_event_protects_active_run_and_resets_bookmark_at_end(repo):
    old=datetime.now(UTC)-timedelta(days=100)
    await repo.db.record_archive_maintenance.insert_one({"_id":"growth-records","owner":"owner","expiresAt":datetime.now(UTC)+timedelta(minutes=1)})
    await repo.db.runs.insert_one({"id":"active","endedAt":None})
    await repo.db.events.insert_many([{"taskId":"task","runId":"active","createdAt":old},{"taskId":"done","runId":"done","createdAt":old}])
    result=await record_purge.purge_expired_records(repo,"events",datetime.now(UTC)-timedelta(days=90),"owner")
    assert result["removed"]==1 and result["protected"]==1
    lease=await repo.db.record_archive_maintenance.find_one({"_id":"growth-records"})
    assert lease.get("cursors",{}).get("events") is None


async def test_audit_archive_retains_trace_fields_and_digest(repo):
    """清理前以压缩摘要保留操作者、对象和请求；不复制正文或任意敏感字段。"""
    old = datetime.now(UTC) - timedelta(days=100)
    await repo.db.record_archive_maintenance.insert_one({"_id": "growth-records", "owner": "owner",
        "expiresAt": datetime.now(UTC)+timedelta(minutes=1)})
    await repo.db.audit.insert_one({"id": "entry", "createdAt": old, "actor": "user", "action": "control:STOPPED",
        "targetId": "task", "requestId": "request", "password": "must-not-archive"})
    await record_purge.purge_expired_records(repo, "audit", datetime.now(UTC)-timedelta(days=90), "owner")
    chunk = await repo.db.audit_archive_chunks.find_one({})
    payload = gzip.decompress(chunk["payload"])
    assert hashlib.sha256(payload).hexdigest() == chunk["sha256"]
    records = json_util.loads(payload)
    assert len(records) == chunk["count"] == 1
    assert records[0]["actor"] == "user" and records[0]["requestId"] == "request"
    assert "password" not in records[0]
