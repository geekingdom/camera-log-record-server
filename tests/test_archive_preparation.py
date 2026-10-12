"""验证慢索引准备不阻塞接收事件循环，准备失败时保留原始分卷。"""

import asyncio
import tarfile
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from camera_logs.logs.storage import HourlyWriter


async def test_slow_archive_index_leaves_event_loop_responsive(tmp_path, monkeypatch):
    """索引读取尚未返回时，事件循环必须能继续调度并释放磁盘模拟器。"""
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    await writer.write(b"first\nsecond\n", received_at=datetime(2026, 9, 14, tzinfo=UTC))
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    original = writer._segment_manifest

    def slow_manifest(segment):
        entered.set()
        try:
            # 有界兜底保证旧实现失败时测试也能收尾；正确实现由事件循环主动释放。
            release.wait(2)
            return original(segment)
        finally:
            completed.set()

    monkeypatch.setattr(writer, "_segment_manifest", slow_manifest)
    closing = asyncio.create_task(writer.close())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert not completed.is_set(), "索引等待期间事件循环被同步读取阻塞"
    finally:
        release.set()
        archive = await closing
    with tarfile.open(archive.path, "r:gz") as bundle:
        assert b"".join(bundle.extractfile(item).read() for item in bundle.getmembers()) == b"first\nsecond\n"


async def test_archive_preparation_error_keeps_raw_data(tmp_path, monkeypatch):
    """线程中的索引失败须传播给关闭调用方，不能发布归档或删除可恢复正文。"""
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    await writer.write(b"recoverable\n", received_at=datetime(2026, 9, 14, tzinfo=UTC))
    original_path = writer.active_path

    def failing_manifest(_segment):
        raise OSError("模拟索引读取失败")

    monkeypatch.setattr(writer, "_segment_manifest", failing_manifest)
    with pytest.raises(OSError, match="模拟索引读取失败"):
        await writer.close()
    assert original_path.read_bytes() == b"recoverable\n"
    assert list(original_path.parent.glob("*.index.jsonl"))
    assert not list(original_path.parent.glob("*.tar.gz"))


async def test_cancelled_close_keeps_preparation_tracked(tmp_path, monkeypatch):
    """取消前台收尾不能丢失封存分卷，下一次收尾须能等待并取回归档。"""
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    await writer.write(b"tracked\n", received_at=datetime(2026, 9, 14, tzinfo=UTC))
    original_path = writer.active_path
    entered, release = threading.Event(), threading.Event()
    original = writer._segment_manifest

    def slow_manifest(segment):
        entered.set()
        release.wait(3)
        return original(segment)

    monkeypatch.setattr(writer, "_segment_manifest", slow_manifest)
    closing = asyncio.create_task(writer.close())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        # 取消必须发生在不可变 spool 已落盘之后；不再依赖旧版每 writer 后台 Task。
        assert list(tmp_path.rglob("*.pending.*.json")) or original_path.exists() or list(tmp_path.rglob("*.tar.gz"))
    finally:
        release.set()
        await writer.close()
    archives = writer.drain_archives()
    archive = archives[0].path if archives else next(tmp_path.rglob("*.tar.gz"))
    with tarfile.open(archive, "r:gz") as bundle:
        assert bundle.extractfile(bundle.getmembers()[0]).read() == b"tracked\n"
    assert not original_path.exists()


async def test_sealing_more_than_two_groups_does_not_wait_for_archive_queue(tmp_path, monkeypatch):
    """归档槽位满时第三次封存只持久化清单，后续正文写入不能等待全局压缩。"""
    from camera_logs.logs import storage

    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    first = datetime(2026, 9, 14, 1, tzinfo=UTC)
    await writer.write(b"first\n", received_at=first)

    scheduled = []

    def record_schedule(target):
        scheduled.append(target)

    monkeypatch.setattr(storage, "schedule_pending_archive", record_schedule)

    for hour in range(2, 5):
        await asyncio.wait_for(writer.write(b"next\n", received_at=first.replace(hour=hour)), .5)

    assert len(scheduled) == 3
    assert all(Path(target).is_file() and ".pending." in Path(target).name for target in scheduled)


async def test_fifty_six_routes_keep_writing_when_bounded_archive_queue_is_full(tmp_path, monkeypatch):
    """56 路跨四小时封存时，八个内存候选耗尽也只能留下磁盘 spool，不能拖住写入。"""
    from camera_logs.logs import compression, storage
    from camera_logs.logs.compression import publish_spooled_archive

    pending = []

    def bounded_schedule(spool):
        # 模拟唯一调度器已有八个低速归档；其余工作只能由不可变磁盘清单承接。
        if len(pending) < compression.MAX_SCHEDULED_ARCHIVES:
            pending.append(Path(spool))

    monkeypatch.setattr(storage, "schedule_pending_archive", bounded_schedule)
    writers = [HourlyWriter(f"task-{route}", f"run-{route}", f"session-{route}", tmp_path,
                            storage_identity="device") for route in range(56)]
    start = datetime(2026, 9, 14, tzinfo=UTC)
    expected = {}
    for hour in range(4):
        for route, writer in enumerate(writers):
            payload = f"route={route} hour={hour}\n".encode()
            expected[(route, hour)] = payload
            await asyncio.wait_for(writer.write(payload, received_at=start.replace(hour=hour)), .5)
    for writer in writers:
        await asyncio.wait_for(writer.close(defer=True), .5)

    spools = sorted(tmp_path.rglob("*.tar.gz.pending.*.json"))
    assert len(pending) == compression.MAX_SCHEDULED_ARCHIVES
    assert len(spools) == 56 * 4
    # 选取一个积压 spool 实际发布，复核原始字节摘要和最终 tar 成员完整性。
    spool = next(item for item in spools if item not in pending)
    from camera_logs.logs.archive_spool import load
    _target, segments = load(spool)
    raw = Path(segments[0]["logPath"])
    original = raw.read_bytes()
    archive = await asyncio.to_thread(publish_spooled_archive, spool, None, False)
    with tarfile.open(archive, "r:gz") as bundle:
        member = bundle.extractfile(segments[0]["logName"])
        assert member is not None and member.read() == original
    assert not raw.exists()
