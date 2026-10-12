"""共享小时包目录回调保持每个分卷身份与既有会话时间。"""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from camera_logs.collection.runtime import SessionRuntime
from camera_logs.logs.hour_download import reusable_hour_archive
from camera_logs.logs.storage import HourArchive, HourlyWriter
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


async def test_shared_hour_archive_reuses_complete_metadata_despite_old_member_size(tmp_path):
    """同小时后续会话扩大共享 tar 时，早期 READY 记录的旧大小不能强制重压下载。"""
    runtime = object.__new__(SessionRuntime)
    runtime.repo = SimpleNamespace(db=AsyncMongoMockClient().db,
                                   settings=SimpleNamespace(log_root=tmp_path, node_id="node"))
    runtime.task = {"id": "task", "runStartedAt": datetime(2026, 9, 8, tzinfo=UTC)}
    runtime.started_at = datetime(2026, 9, 8, 2, tzinfo=UTC)
    runtime.paths, runtime.catalog_dirty, runtime.catalog_ready = {}, {}, set()
    runtime.catalog_lock = asyncio.Lock()
    instant = datetime(2026, 9, 8, 1, tzinfo=UTC)
    first = HourlyWriter("task", "run-a", "session-a", tmp_path, storage_identity="device")
    await first.write(b"first session\n", received_at=instant)
    first_archive = await first.close()
    await runtime.on_archive(first_archive)
    second = HourlyWriter("task", "run-b", "session-b", tmp_path, storage_identity="device")
    await second.write(b"second session\n", received_at=instant)
    second_archive = await second.close()
    await runtime.on_archive(second_archive)

    records = [record async for record in runtime.repo.db.files.find({})]
    assert len(records) == 2 and first_archive.path == second_archive.path
    assert all(record["archiveBytes"] == second_archive.path.stat().st_size for record in records)
    stale_records = [record | {"archiveBytes": record["archiveBytes"] - 1} if index == 0 else record
                     for index, record in enumerate(records)]
    sources = [(record, record, Path(record["path"]), False) for record in stale_records]
    assert reusable_hour_archive(sources) == second_archive.path

    pinned = tmp_path / "pinned-hour.tar.gz"
    os.link(second_archive.path, pinned)
    replacement = tmp_path / "replacement.tar.gz"
    replacement.write_bytes(second_archive.path.read_bytes())
    os.replace(replacement, second_archive.path)
    # 发布顺序中 tar 会先于 metadata 替换；即使下载刚固定到新 tar，旧清单也
    # 不能用来证明该版本包含完整选择集。
    assert reusable_hour_archive(sources) is None
    pinned_sources = [(record, record, pinned, False) for record in records]
    assert reusable_hour_archive(pinned_sources) is None
