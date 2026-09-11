"""旧运行事件时间回填只能使用明确的 BSON 检测时间，且必须支持安全续跑。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from camera_logs.common.runtime_event_time import backfill_runtime_event_created_at
from mongomock_motor import AsyncMongoMockClient


@pytest.mark.asyncio
async def test_runtime_event_time_preview_apply_and_limit_preserve_existing_values():
    """预览不写库；回填有界、保留有效 createdAt 并报告仍待处理数量。"""
    db = AsyncMongoMockClient().db
    detected = datetime(2026, 9, 11, 5, 10, tzinfo=UTC)
    existing = datetime(2026, 9, 10, tzinfo=UTC)
    await db.events.insert_many([
        {"_id": "missing", "detectedAt": detected},
        {"_id": "null", "createdAt": None, "detectedAt": detected},
        {"_id": "existing", "createdAt": existing, "detectedAt": detected},
        {"_id": "no-time"},
        {"_id": "not-date", "createdAt": None, "detectedAt": "2026-09-11"},
    ])

    preview = await backfill_runtime_event_created_at(db, max_records=1)
    assert preview == {
        "apply": False, "matched": 1, "updated": 0, "remaining": 2,
        "unmigratable": 2, "limitReached": True, "maxRecords": 1,
    }
    assert (await db.events.find_one({"_id": "missing"})).get("createdAt") is None

    applied = await backfill_runtime_event_created_at(db, apply=True, max_records=1)
    assert applied["matched"] == applied["updated"] == 1
    assert applied["remaining"] == 1 and applied["limitReached"]
    assert (await db.events.find_one({"_id": "existing"}))["createdAt"].replace(tzinfo=UTC) == existing

    completed = await backfill_runtime_event_created_at(db, apply=True)
    assert completed["matched"] == completed["updated"] == 1
    assert completed["remaining"] == 0 and not completed["limitReached"]
    for identifier in ("missing", "null"):
        assert (await db.events.find_one({"_id": identifier}))["createdAt"].replace(tzinfo=UTC) == detected
    repeated = await backfill_runtime_event_created_at(db, apply=True)
    assert repeated["matched"] == repeated["updated"] == repeated["remaining"] == 0


@pytest.mark.asyncio
async def test_runtime_event_time_cas_does_not_overwrite_concurrent_created_at():
    """读取后的并发补齐或检测时间变化必须让本次更新计数保持为零。"""
    db = AsyncMongoMockClient().db
    detected = datetime(2026, 9, 11, 5, 10, tzinfo=UTC)
    concurrent = datetime(2026, 9, 11, 6, tzinfo=UTC)
    await db.events.insert_one({"_id": "race", "createdAt": None, "detectedAt": detected})

    collection = db.events
    changed = False

    async def update_one(query, update, *update_args, **update_kwargs):
        nonlocal changed
        if not changed:
            changed = True
            await collection.update_one({"_id": "race"}, {"$set": {"createdAt": concurrent}})
        return await collection.update_one(query, update, *update_args, **update_kwargs)

    proxy = SimpleNamespace(find=collection.find, count_documents=collection.count_documents, update_one=update_one)
    result = await backfill_runtime_event_created_at(SimpleNamespace(events=proxy), apply=True)

    assert result["matched"] == 1 and result["updated"] == 0 and result["remaining"] == 0
    assert (await db.events.find_one({"_id": "race"}))["createdAt"].replace(tzinfo=UTC) == concurrent


@pytest.mark.asyncio
async def test_runtime_event_time_cas_does_not_write_changed_detected_time_or_date_array():
    """检测时间改写、并发变数组和日期数组都不能成为 createdAt 的来源。"""
    db = AsyncMongoMockClient().db
    detected = datetime(2026, 9, 11, 5, 10, tzinfo=UTC)
    await db.events.insert_many([
        {"_id": "changed-detected", "createdAt": None, "detectedAt": detected},
        {"_id": "date-array", "createdAt": None, "detectedAt": [detected]},
        {"_id": "created-array", "createdAt": [None], "detectedAt": detected},
    ])
    collection = db.events
    changed = False

    async def update_one(query, update, *update_args, **update_kwargs):
        nonlocal changed
        if not changed:
            changed = True
            # Mongo 标量等值会匹配包含该值的数组；更新 CAS 也必须明确排除数组。
            await collection.update_one({"_id": "changed-detected"}, {"$set": {"detectedAt": [detected]}})
        return await collection.update_one(query, update, *update_args, **update_kwargs)

    proxy = SimpleNamespace(find=collection.find, count_documents=collection.count_documents, update_one=update_one)
    result = await backfill_runtime_event_created_at(SimpleNamespace(events=proxy), apply=True)

    assert result["matched"] == 1 and result["updated"] == 0
    changed_row = await collection.find_one({"_id": "changed-detected"})
    assert changed_row["createdAt"] is None
    assert changed_row["detectedAt"][0].replace(tzinfo=UTC) == detected
    assert (await collection.find_one({"_id": "date-array"}))["createdAt"] is None
    assert (await collection.find_one({"_id": "created-array"}))["createdAt"] == [None]


@pytest.mark.asyncio
async def test_runtime_event_time_rejects_unbounded_or_invalid_batch_settings():
    """迁移必须保持批次及单次总量上限，避免运维误触全表长事务。"""
    db = AsyncMongoMockClient().db
    with pytest.raises(ValueError):
        await backfill_runtime_event_created_at(db, batch_size=0)
    with pytest.raises(ValueError):
        await backfill_runtime_event_created_at(db, max_records=100_001)
