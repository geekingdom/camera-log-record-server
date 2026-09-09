"""小时共享归档与严格 10 MiB 分卷的存储回归测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from camera_logs.logs.compression import publish_hour_archive
from camera_logs.logs.storage import MAX_LOG_BYTES, HourlyWriter


def _metadata(path):
    return json.loads(path.with_name(path.name + ".metadata.json").read_text())


def test_hourly_writer_requires_an_explicit_storage_identity(tmp_path):
    with pytest.raises(TypeError):
        HourlyWriter("task", "run", "session", tmp_path)


def test_utc_server_evening_is_next_shanghai_archive_day():
    """用户服务器 UTC 晚间时间应归入下一北京时间日期，不能按主机时区分桶。"""
    instant = datetime(2026, 9, 9, 18, 30, 12, tzinfo=UTC)
    hour = HourlyWriter._hour_of(instant, ZoneInfo("Asia/Shanghai"))
    assert hour.isoformat() == "2026-09-10T02:00:00+08:00"


async def test_shared_hour_directory_reuses_one_tar_and_keeps_indexes_outside(tmp_path):
    instant = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
    first = HourlyWriter("task", "run-a", "session-a", tmp_path, storage_identity="device")
    second = HourlyWriter("task", "run-b", "session-b", tmp_path, storage_identity="device")
    await first.write(b"first\n", received_at=instant)
    one = await first.close()
    await second.write(b"second\n", received_at=instant)
    two = await second.close()
    assert one.path == two.path
    assert one.path.parent == tmp_path / "resources" / "device" / "task" / "2026" / "09" / "08" / "09"
    with tarfile.open(one.path, "r:gz") as bundle:
        assert all(name.endswith(".log") for name in bundle.getnames())
        assert b"".join(bundle.extractfile(name).read() for name in bundle.getnames()) == b"first\nsecond\n"
    metadata = _metadata(one.path)
    assert metadata["formatVersion"] == 2
    assert [member["runId"] for member in metadata["members"]] == ["run-a", "run-b"]
    assert all((one.path.parent / member["indexName"]).exists() for member in metadata["members"])


async def test_single_input_larger_than_10_mib_is_split_at_strict_limit(tmp_path):
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    data = b"a" * (MAX_LOG_BYTES + 17)
    positions = await writer.write_many([(data, datetime(2026, 9, 8, 1, tzinfo=UTC))])
    archive = await writer.close()
    assert [position.length for position in positions] == [MAX_LOG_BYTES, 17]
    assert [position.source_index for position in positions] == [0, 0]
    assert [position.source_offset for position in positions] == [0, MAX_LOG_BYTES]
    assert archive.raw_size == MAX_LOG_BYTES
    metadata = _metadata(archive.path)
    assert [member["rawSize"] for member in metadata["members"]] == [MAX_LOG_BYTES, 17]
    with tarfile.open(archive.path, "r:gz") as bundle:
        assert b"".join(bundle.extractfile(name).read() for name in bundle.getnames()) == data


async def test_hour_switch_archives_all_sealed_parts_and_reports_each_member(tmp_path):
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    first = datetime(2026, 9, 8, 1, 59, 59, tzinfo=UTC)
    second = datetime(2026, 9, 8, 2, 0, tzinfo=UTC)
    await writer.write_many([(b"one", first), (b"two", second)])
    await writer.close()
    sealed = writer.drain_archives()
    assert len(sealed) == 1
    assert sealed[0].member_name and sealed[0].index_path
    paths = list(tmp_path.rglob("*.tar.gz"))
    assert len(paths) == 2


async def test_metadata_records_index_digest_and_sequence(tmp_path):
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    await writer.write(b"durable\n")
    writer._last_sync -= 2
    await writer.sync_due()
    archive = await writer.close()
    member = _metadata(archive.path)["members"][0]
    index = archive.path.parent / member["indexName"]
    assert member["indexBytes"] == index.stat().st_size
    assert member["indexSha256"] == hashlib.sha256(index.read_bytes()).hexdigest()
    assert member["firstSequence"] == member["lastSequence"] == 1


async def test_successful_archive_deletes_only_raw_log_and_preserves_index(tmp_path):
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    await writer.write(b"delete raw only\n")
    archive = await writer.close()
    member = _metadata(archive.path)["members"][0]
    assert not (archive.path.parent / member["logName"]).exists()
    assert (archive.path.parent / member["indexName"]).exists()
    assert archive.path.exists()
    assert archive.path.with_name(archive.path.name + ".metadata.json").exists()


def test_failed_archive_keeps_raw_log_and_index(tmp_path, monkeypatch):
    async def fail(_segments, _target):
        raise OSError("injected archive failure")

    monkeypatch.setattr("camera_logs.logs.storage.compress_hour", fail)
    async def scenario():
        writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
        await writer.write(b"preserve on failure\n")
        with pytest.raises(OSError, match="injected archive failure"):
            await writer.close()

    asyncio.run(scenario())
    assert list(tmp_path.rglob("*.log"))
    assert list(tmp_path.rglob("*.index.jsonl"))
    assert not list(tmp_path.rglob("*.tar.gz"))


def test_index_reservation_race_removes_new_log_before_advancing_part(tmp_path, monkeypatch):
    """索引竞争不能留下无索引的独占正文文件。"""
    original_touch, raced = Path.touch, False

    def race(path, *args, **kwargs):
        nonlocal raced
        if path.name.endswith("part-000001.index.jsonl") and not raced:
            raced = True
            raise FileExistsError(path)
        return original_touch(path, *args, **kwargs)

    monkeypatch.setattr(Path, "touch", race)

    async def scenario():
        writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
        await writer.write(b"race")
        assert writer.active_path.name.endswith("part-000002.log")
        await writer.close()

    asyncio.run(scenario())
    assert not list(tmp_path.rglob("*part-000001.log"))


def test_metadata_publish_failure_recovers_pending_members_in_next_session(tmp_path, monkeypatch):
    """tar 已替换、metadata 未发布时，新会话继续归档不得遗失或重复前一分卷。"""
    target = tmp_path / "hour.tar.gz"

    def segment(number, content):
        log = tmp_path / f"part-{number:06d}.log"
        index = tmp_path / f"part-{number:06d}.index.jsonl"
        log.write_bytes(content)
        index.write_text('{"sequence":1}\n')
        return {"taskId": "task", "runId": f"run-{number}", "sessionId": f"session-{number}",
            "hourStart": "2026-09-08T01:00:00+00:00", "rawSize": len(content),
            "sha256": hashlib.sha256(content).hexdigest(), "firstSequence": 1, "lastSequence": 1,
            "logPath": str(log), "logName": log.name, "indexName": index.name,
            "indexSha256": hashlib.sha256(index.read_bytes()).hexdigest(), "indexBytes": index.stat().st_size}

    first = segment(1, b"first")
    original_replace, failed = os.replace, False

    def fail_metadata(source, destination):
        nonlocal failed
        if str(destination) == str(target) + ".metadata.json" and not failed:
            failed = True
            raise OSError("metadata replace failed")
        return original_replace(source, destination)

    monkeypatch.setattr("camera_logs.logs.compression.os.replace", fail_metadata)
    with pytest.raises(OSError, match="metadata replace failed"):
        publish_hour_archive([first], target)
    assert target.exists() and (tmp_path / "part-000001.log").exists()
    assert target.with_name(target.name + ".pending.json").exists()

    second = segment(2, b"second")
    publish_hour_archive([second], target)
    metadata = _metadata(target)
    assert [member["logName"] for member in metadata["members"]] == [first["logName"], second["logName"]]
    with tarfile.open(target, "r:gz") as bundle:
        assert [bundle.extractfile(name).read() for name in bundle.getnames()] == [b"first", b"second"]
    assert not list(tmp_path.glob("*.log"))
    assert not target.with_name(target.name + ".pending.json").exists()


def test_storage_scenarios(tmp_path):
    asyncio.run(test_shared_hour_directory_reuses_one_tar_and_keeps_indexes_outside(tmp_path / "shared"))
    asyncio.run(test_single_input_larger_than_10_mib_is_split_at_strict_limit(tmp_path / "split"))
    asyncio.run(test_hour_switch_archives_all_sealed_parts_and_reports_each_member(tmp_path / "hours"))
    asyncio.run(test_metadata_records_index_digest_and_sequence(tmp_path / "metadata"))
    asyncio.run(test_successful_archive_deletes_only_raw_log_and_preserves_index(tmp_path / "delete"))
