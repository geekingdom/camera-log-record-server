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
from camera_logs.logs import storage
from camera_logs.logs.compression import publish_hour_archive
from camera_logs.logs.storage import MAX_LOG_BYTES, HourArchive, HourlyWriter


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
    assert one.path.parent == tmp_path / "device" / "task-task" / "2026-09-08" / "09"
    with tarfile.open(one.path, "r:gz") as bundle:
        assert all(name.endswith(".log") for name in bundle.getnames())
        assert b"".join(bundle.extractfile(name).read() for name in bundle.getnames()) == b"first\nsecond\n"
    metadata = _metadata(one.path)
    assert metadata["formatVersion"] == 2
    assert [member["runId"] for member in metadata["members"]] == ["run-a", "run-b"]
    assert all((one.path.parent / member["indexName"]).exists() for member in metadata["members"])


async def test_hour_archive_name_contains_safe_task_ip_and_shanghai_hour_range(tmp_path):
    """小时包文件名必须能直接识别任务、设备和上海自然小时范围。"""
    writer = HourlyWriter(
        "task-id", "run", "session", tmp_path, storage_identity="device",
        task_name="现场/主机:日志", device_ip="2001:db8::34",
    )
    # UTC 01:05 对应上海 09:05，归档范围应为 09:00-10:00。
    await writer.write(b"line\n", received_at=datetime(2026, 9, 8, 1, 5, tzinfo=UTC))
    archive = await writer.close()

    assert archive.path.name == (
        "现场-主机-日志-2001-db8--34-20260908090000-20260908100000.tar.gz"
    )
    assert archive.path.exists()


async def test_same_named_tasks_use_full_ids_in_separate_hour_directories(tmp_path):
    """同名任务的完整 ID 必须进入目录，避免共享小时包而混淆下载范围。"""
    instant = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
    first = HourlyWriter("full-task-id-a", "run-a", "session-a", tmp_path,
                         storage_identity="device-a", task_name="采集任务")
    second = HourlyWriter("full-task-id-b", "run-b", "session-b", tmp_path,
                          storage_identity="device-a", task_name="采集任务")
    await first.write(b"first\n", received_at=instant)
    await second.write(b"second\n", received_at=instant)
    first_archive, second_archive = await first.close(), await second.close()

    assert first_archive.path.parent == tmp_path / "device-a" / "采集任务-full-task-id-a" / "2026-09-08" / "09"
    assert second_archive.path.parent == tmp_path / "device-a" / "采集任务-full-task-id-b" / "2026-09-08" / "09"
    assert first_archive.path != second_archive.path


async def test_task_rename_keeps_each_writer_directory_immutable_and_hour_stable(tmp_path):
    """运行创建后名称不回溯；改名后的同小时归档仍保留相同自然小时语义。"""
    instant = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
    old = HourlyWriter("full-task-id", "run-a", "session-a", tmp_path,
                       storage_identity="device-a", task_name="改名前")
    renamed = HourlyWriter("full-task-id", "run-b", "session-b", tmp_path,
                           storage_identity="device-a", task_name="改名后")
    await old.write(b"old\n", received_at=instant)
    await renamed.write(b"new\n", received_at=instant)
    old_archive, renamed_archive = await old.close(), await renamed.close()

    assert old_archive.path.parent != renamed_archive.path.parent
    assert old_archive.hour_start == renamed_archive.hour_start


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


async def test_hour_switch_manifest_uses_incremental_index_digest(tmp_path, monkeypatch):
    """跨整点封存直接使用已追加的索引摘要，不能重新读取已关闭索引。"""
    original_open = Path.open

    def forbid_sealed_index_read(path, *args, **kwargs):
        if path.name.endswith(".index.jsonl") and "r" in (args[0] if args else kwargs.get("mode", "r")):
            raise AssertionError("归档 manifest 重新读取了已关闭索引")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid_sealed_index_read)
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    first = datetime(2026, 9, 8, 1, 59, 59, tzinfo=UTC)
    await writer.write(b"previous\n", received_at=first)
    await writer.write(b"current\n", received_at=first.replace(hour=2))
    assert writer.active_path is not None and writer.active_path.read_bytes() == b"current\n"
    await writer.close()


async def test_metadata_records_index_digest_and_sequence(tmp_path):
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    await writer.write(b"durable\n")
    writer._last_sync -= 2
    assert await writer.sync_due() is True
    assert await writer.sync_due() is False
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


async def test_deferred_archive_notifies_catalog_callback_without_a_later_write(tmp_path, monkeypatch):
    """收尾延后归档成功后立即通知目录，不能等待下一次采集或维护扫描。"""
    published, completed = [], asyncio.get_running_loop().create_future()

    def schedule(_spool, **_kwargs):
        return completed

    async def on_archive(archive):
        published.append(archive)

    monkeypatch.setattr("camera_logs.logs.storage.schedule_pending_archive", schedule)
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device",
                          on_archive=on_archive)
    await writer.write(b"deferred\n")
    assert await writer.close(defer=True) is None

    completed.set_result(tmp_path / "published.tar.gz")
    for _ in range(10):
        if published:
            break
        await asyncio.sleep(0)

    assert len(published) == 1
    assert published[0].task_id == "task" and published[0].raw_size == len(b"deferred\n")


async def test_deferred_archive_catalog_callback_times_out_without_retaining_notification_task(tmp_path, monkeypatch):
    """目录数据库无响应不能长期保留已关闭会话，已发布归档仍由维护扫描补偿。"""
    completed, started, release = (asyncio.get_running_loop().create_future(), asyncio.Event(), asyncio.Event())

    async def on_archive(_archive):
        started.set()
        await release.wait()

    monkeypatch.setattr("camera_logs.logs.storage.schedule_pending_archive", lambda _spool, **_kwargs: completed)
    monkeypatch.setattr("camera_logs.logs.storage.ARCHIVE_CATALOG_CALLBACK_TIMEOUT_SECONDS", .01)
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device", on_archive=on_archive)
    await writer.write(b"timeout\n")
    await writer.close(defer=True)
    completed.set_result(tmp_path / "published.tar.gz")
    await started.wait()
    await asyncio.sleep(.03)

    assert writer._archive_notification_task is None


async def test_deferred_archive_catalog_notifies_every_member_of_large_hour_batch(tmp_path, monkeypatch):
    """单小时超过八个分卷时，完整批次仍逐项登记为 READY，不能按分卷丢弃。"""
    completed = asyncio.get_running_loop().create_future()
    published = []

    async def on_archive(archive):
        published.append(archive.member_name)

    monkeypatch.setattr(storage, "MAX_LOG_BYTES", 1)
    monkeypatch.setattr(storage, "schedule_pending_archive", lambda _spool, **_kwargs: completed)
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device", on_archive=on_archive)
    await writer.write_many([(b"x", datetime(2026, 9, 8, 1, tzinfo=UTC)) for _ in range(60)])
    assert await writer.close(defer=True) is None

    completed.set_result(tmp_path / "published.tar.gz")
    while len(published) < 60 and writer._archive_notification_task is None:
        await asyncio.sleep(0)
    if writer._archive_notification_task is not None:
        await writer._archive_notification_task

    assert len(published) == 60
    assert len(set(published)) == 60


async def test_hung_archive_catalog_keeps_only_eight_pending_batches(tmp_path, monkeypatch):
    """Mongo 回调挂起时队列按批次限八个，额外已发布批次交由 metadata 维护补偿。"""
    started, release = asyncio.Event(), asyncio.Event()

    async def on_archive(_archive):
        started.set()
        await release.wait()

    monkeypatch.setattr(storage, "ARCHIVE_CATALOG_CALLBACK_TIMEOUT_SECONDS", 1)
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device", on_archive=on_archive)
    batches = tuple(
        (HourArchive("task", "run", "session", datetime(2026, 9, 8, 1, tzinfo=UTC), tmp_path / f"{index}.tar.gz",
                     1, str(index), 1, 1),)
        for index in range(storage.MAX_PENDING_ARCHIVE_NOTIFICATIONS + 2)
    )
    writer._queue_archive_notifications(batches[0])
    await started.wait()
    for batch in batches[1:]:
        writer._queue_archive_notifications(batch)

    assert len(writer._archive_notifications) == storage.MAX_PENDING_ARCHIVE_NOTIFICATIONS
    release.set()
    assert writer._archive_notification_task is not None
    await writer._archive_notification_task


def test_failed_archive_keeps_raw_log_and_index(tmp_path, monkeypatch):
    async def fail(_spool):
        raise OSError("injected archive failure")

    monkeypatch.setattr("camera_logs.logs.storage.compress_spool", fail)
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
