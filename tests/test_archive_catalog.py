"""共享小时包目录回调保持每个分卷身份与既有会话时间。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from camera_logs.collection.runtime import SessionRuntime
from camera_logs.logs.storage import HourArchive
from mongomock_motor import AsyncMongoMockClient


async def test_archive_callback_keeps_member_ids_and_original_session_times(tmp_path):
    runtime = object.__new__(SessionRuntime)
    runtime.repo = SimpleNamespace(db=AsyncMongoMockClient().db,
                                   settings=SimpleNamespace(log_root=tmp_path, node_id="node"))
    runtime.task = {"id": "task", "runStartedAt": datetime(2026, 9, 8, tzinfo=UTC)}
    runtime.started_at = datetime(2026, 9, 8, 2, tzinfo=UTC)
    runtime.paths, runtime.catalog_dirty, runtime.catalog_ready = {}, {}, set()
    runtime.catalog_lock = asyncio.Lock()
    archive_path = tmp_path / "hour.tar.gz"
    archive_path.write_bytes(b"synthetic-archive")
    original = datetime(2026, 9, 8, 1, tzinfo=UTC)
    for number in (1, 2):
        log = tmp_path / f"task-part-{number:06d}.log"
        identifier = runtime.file_id(log)
        await runtime.repo.db.files.insert_one({"id": identifier, "sessionStartedAt": original})
        archive = HourArchive("task", "run", "session", original, archive_path, 10, "digest",
                              number, number, log, log.name, log.with_suffix(".index.jsonl"))
        await runtime.on_archive(archive)
        record = await runtime.repo.db.files.find_one({"id": identifier})
        assert record["sessionStartedAt"].replace(tzinfo=UTC) == original
        assert record["archiveMember"] == log.name
        assert record["segmentNumber"] == number
        assert record["status"] == "READY"
    records = [doc async for doc in runtime.repo.db.files.find({})]
    assert len(records) == 2
    assert len({doc["archiveGroupId"] for doc in records}) == 1
