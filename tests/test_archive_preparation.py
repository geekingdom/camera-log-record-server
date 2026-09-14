"""验证慢索引准备不阻塞接收事件循环，准备失败时保留原始分卷。"""

import asyncio
import tarfile
import threading
from datetime import UTC, datetime

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
        assert writer._archive_tasks
    finally:
        release.set()
        await writer.close()
    archives = writer.drain_archives()
    assert len(archives) == 1
    with tarfile.open(archives[0].path, "r:gz") as bundle:
        assert bundle.extractfile(bundle.getmembers()[0]).read() == b"tracked\n"
    assert not original_path.exists()
