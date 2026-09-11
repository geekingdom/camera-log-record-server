"""认证历史升级必须支持预览、分批续跑，并保留已生效的到期策略。"""

from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from camera_logs.common.authentication_retention import backfill_authentication_expiry
from mongomock_motor import AsyncMongoMockClient


@pytest.mark.asyncio
async def test_backfill_preview_batches_and_retry_preserve_existing_expiry():
    db = AsyncMongoMockClient().db
    stamp = datetime(2026, 9, 11, tzinfo=UTC)
    await db.authentication_records.insert_many([
        {"_id": str(index), "createdAt": stamp} for index in range(5)
    ] + [{"_id": ObjectId(), "createdAt": stamp},
         {"_id": "existing", "createdAt": stamp, "expiresAt": stamp}, {"_id": "invalid"}])
    preview = await backfill_authentication_expiry(db, 90, batch_size=2)
    assert preview["eligible"] == 6 and preview["updated"] == 0
    assert await db.authentication_records.count_documents({"expiresAt": {"$exists": True}}) == 1
    result = await backfill_authentication_expiry(db, 90, apply=True, batch_size=2, max_records=3)
    assert result["updated"] == 3 and result["limitReached"]
    assert (await backfill_authentication_expiry(db, 90, apply=True))["updated"] == 3
    row = await db.authentication_records.find_one({"_id": "0"})
    assert row["expiresAt"].replace(tzinfo=UTC) == stamp + timedelta(days=90)
    row = await db.authentication_records.find_one({"_id": "existing"})
    assert row["expiresAt"].replace(tzinfo=UTC) == stamp
    assert (await backfill_authentication_expiry(db, 90, apply=True))["updated"] == 0


@pytest.mark.asyncio
async def test_null_expiry_is_previewed_and_backfilled_once():
    db = AsyncMongoMockClient().db
    stamp = datetime(2026, 9, 11, tzinfo=UTC)
    await db.authentication_records.insert_one({"_id": "null", "createdAt": stamp, "expiresAt": None})
    assert (await backfill_authentication_expiry(db, 90))["eligible"] == 1
    assert (await db.authentication_records.find_one({"_id": "null"}))["expiresAt"] is None
    assert (await backfill_authentication_expiry(db, 90, apply=True))["updated"] == 1
    row = await db.authentication_records.find_one({"_id": "null"})
    assert row["expiresAt"].replace(tzinfo=UTC) == stamp + timedelta(days=90)
    assert (await backfill_authentication_expiry(db, 90, apply=True))["updated"] == 0


@pytest.mark.asyncio
async def test_disabled_retention_cannot_silently_expire_history():
    with pytest.raises(ValueError):
        await backfill_authentication_expiry(AsyncMongoMockClient().db, 0, apply=True)
