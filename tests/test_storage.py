"""小时写入和归档测试：验证原始字节保真、隔离路径和跨小时分片。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tarfile
from datetime import UTC, datetime

import pytest
from camera_logs.logs.naming import safe_device_address, safe_filename_component
from camera_logs.logs.storage import HourlyWriter


def test_hourly_writer_requires_an_explicit_storage_identity(tmp_path):
    """开发期不允许回退旧目录，调用方必须明确提供设备存储身份。"""
    with pytest.raises(TypeError):
        HourlyWriter("task", "run", "session", tmp_path)


def test_hourly_writer_preserves_byte_order_and_publishes_verified_archive(tmp_path):
    async def scenario():
        writer = HourlyWriter(
            task_id="task-a",
            run_id="run-a",
            session_id="session-a",
            root=tmp_path,
            storage_identity="device-a",
            now=lambda: datetime(2026, 9, 8, 1, 59, 59, tzinfo=UTC),
        )
        await writer.write(b"first\n")
        archive = await writer.rotate(datetime(2026, 9, 8, 2, 0, 0, tzinfo=UTC))
        await writer.write(b"second\n")
        await writer.close()
        return archive

    archive = asyncio.run(scenario())

    assert archive.raw_size == len(b"first\n")
    assert archive.sha256 == hashlib.sha256(b"first\n").hexdigest()
    assert archive.path.suffixes[-2:] == [".tar", ".gz"]
    assert archive.path.exists()
    assert not archive.path.with_suffix(archive.path.suffix + ".partial").exists()
    with tarfile.open(archive.path, "r:gz") as bundle:
        names = bundle.getnames()
        log_name = next(name for name in names if name.endswith(".log"))
        assert bundle.extractfile(log_name).read() == b"first\n"
        assert "manifest.json" in names


def test_hourly_writer_uses_separate_paths_for_separate_tasks(tmp_path):
    async def scenario():
        one = HourlyWriter("one", "run", "session", tmp_path, storage_identity="device-one")
        two = HourlyWriter("two", "run", "session", tmp_path, storage_identity="device-two")
        await one.write(b"one")
        await two.write(b"two")
        return await one.close(), await two.close()

    first, second = asyncio.run(scenario())
    assert first.path != second.path
    assert "/one/" in first.path.as_posix()
    assert "/two/" in second.path.as_posix()


def test_same_device_identity_shares_parent_directory_while_tasks_remain_isolated(tmp_path):
    """同一海康身份归到共同父目录，任务、运行和会话仍各自隔离。"""
    async def scenario():
        first = HourlyWriter("task-one", "run-one", "session-one", tmp_path, storage_identity="device-hash")
        second = HourlyWriter("task-two", "run-two", "session-two", tmp_path, storage_identity="device-hash")
        await first.write(b"one")
        await second.write(b"two")
        return await first.close(), await second.close()

    first, second = asyncio.run(scenario())
    shared_parent = tmp_path / "resources" / "device-hash"
    assert first.path.is_relative_to(shared_parent / "task-one")
    assert second.path.is_relative_to(shared_parent / "task-two")
    assert first.path != second.path


def test_batch_write_keeps_offsets_and_seals_previous_hour(tmp_path):
    async def scenario():
        writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
        first = datetime(2026, 9, 8, 1, 59, 59, tzinfo=UTC)
        second = datetime(2026, 9, 8, 2, 0, 0, tzinfo=UTC)
        positions = await writer.write_many([(b"abc", first), (b"de", first), (b"f", second)])
        await writer.close()
        sealed = writer.drain_archives()
        return positions, sealed

    positions, sealed = asyncio.run(scenario())
    assert [item.sequence for item in positions] == [1, 2, 3]
    assert [item.offset for item in positions] == [0, 3, 0]
    assert len(sealed) == 1
    assert sealed[0].raw_size == 5


def test_archive_name_contains_safe_task_ip_shanghai_range_and_session_part(tmp_path):
    async def scenario():
        writer = HourlyWriter(
            "task", "run-12345678", "session-abcdefgh", tmp_path,
            task_name="机房/主摄像机: A", device_ip="2001:db8::10", storage_identity="device",
        )
        await writer.write(b"line\n", received_at=datetime(2026, 9, 8, 1, 0, tzinfo=UTC))
        return await writer.close()

    archive = asyncio.run(scenario())
    assert archive.path.name == (
        "机房-主摄像机- A-2001-db8--10-20260908090000-20260908100000-"
        "run-1234-session--part-001.tar.gz"
    )
    with tarfile.open(archive.path, "r:gz") as bundle:
        assert bundle.getnames()[:2] == [
            archive.path.name.removesuffix(".tar.gz") + ".log",
            archive.path.name.removesuffix(".tar.gz") + ".index.jsonl",
        ]


def test_safe_filename_component_limits_utf8_and_blocks_path_characters():
    value = safe_filename_component("../" + "摄" * 40 + ":*?\\log", max_utf8_bytes=31)
    assert len(value.encode("utf-8")) <= 31
    assert all(character not in value for character in '/\\:*?')
    assert safe_device_address("2001:db8::1") == "2001-db8--1"


def test_naming_advances_existing_part_numbers_without_overwrite(tmp_path):
    async def scenario():
        instant = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
        base = tmp_path / "resources" / "device" / "task" / "run" / "session" / "2026" / "09" / "08" / "09"
        base.mkdir(parents=True)
        (base / "任务-10.0.0.1-20260908090000-20260908100000-run-sess-part-007.tar.gz").write_bytes(b"prior")
        first = HourlyWriter("task", "run", "session", tmp_path, task_name="任务", device_ip="10.0.0.1", storage_identity="device")
        await first.write(b"one", received_at=instant)
        first_archive = await first.close()
        second = HourlyWriter("task", "run", "session", tmp_path, task_name="任务", device_ip="10.0.0.1", storage_identity="device")
        await second.write(b"two", received_at=instant)
        second_archive = await second.close()
        return first_archive, second_archive

    first, second = asyncio.run(scenario())
    assert first.path.name.endswith("-part-008.tar.gz")
    assert second.path.name.endswith("-part-009.tar.gz")
    assert first.path.read_bytes() != second.path.read_bytes()


async def test_periodic_sync_advances_durable_watermark_and_archive_checks_index(tmp_path):
    """同步水位只在 fsync 后推进，归档清单同时覆盖正文与索引。"""
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    await writer.write(b"durable\n")
    assert writer.snapshot()["bytesDurable"] == 0
    writer._last_sync -= 2
    await writer.sync_due()
    assert writer.snapshot()["bytesDurable"] == len(b"durable\n")
    archive = await writer.close()
    with tarfile.open(archive.path, "r:gz") as bundle:
        manifest = json.load(bundle.extractfile("manifest.json"))
        member = next(item for item in bundle.getmembers() if item.name.endswith(".index.jsonl"))
        raw = bundle.extractfile(member).read()
        assert manifest["indexBytes"] == len(raw)
        assert manifest["indexSha256"] == hashlib.sha256(raw).hexdigest()
