"""批量写入在慢盘、部分写入和取消时仍保持源顺序及文件归属。"""

import asyncio
import json
import threading
import time
from datetime import UTC, datetime

import pytest
from camera_logs.logs.storage import HourlyWriter


async def test_batch_uses_one_append_and_preserves_each_source_position(tmp_path, monkeypatch):
    """多个小包合并一次文件写入，旁路索引和回调仍保留每包身份。"""
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    stamp = datetime(2026, 9, 15, tzinfo=UTC)
    await writer.write(b"head\n", received_at=stamp)
    original = writer._handle

    class CountWrites:
        calls = 0

        def write(self, data):
            self.calls += 1
            return original.write(data)

        def __getattr__(self, name):
            return getattr(original, name)

    handle = CountWrites()
    writer._handle = handle
    chunks = [(f"line-{i}\n".encode(), stamp) for i in range(120)]
    positions = await writer.write_many(chunks)
    assert writer.active_path.read_bytes() == b"head\n" + b"".join(data for data, _ in chunks)
    assert [p.source_index for p in positions] == list(range(120))
    indexes = [json.loads(line) for line in writer._index.read_text().splitlines()]
    assert [row["sequence"] for row in indexes] == list(range(1, 122))
    assert handle.calls == 1
    await writer.close()


async def test_cancelled_slow_write_keeps_lock_until_thread_finishes(tmp_path):
    """取消等待者不能让close与仍在执行的文件写线程并发。"""
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    await writer.write(b"head\n")
    original, entered, release = writer._handle, threading.Event(), threading.Event()

    class SlowWrite:
        def write(self, data):
            entered.set()
            release.wait(3)
            return original.write(data)

        def __getattr__(self, name):
            return getattr(original, name)

    writer._handle = SlowWrite()
    writing = asyncio.create_task(writer.write(b"tail\n"))
    assert await asyncio.to_thread(entered.wait, 2)
    writing.cancel()
    closing = asyncio.create_task(writer.close())
    try:
        await asyncio.sleep(.05)
        assert not closing.done()
        assert not original.closed
    finally:
        release.set()
        await asyncio.gather(writing, closing, return_exceptions=True)
    assert writing.cancelled()
    assert closing.exception() is None


async def test_partial_write_error_poison_writer_without_replaying_batch(tmp_path):
    """部分字节写入后失败必须保留原文件并拒绝继续，不能归档成完整文件。"""
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    await writer.write(b"head\n")
    original, path = writer._handle, writer.active_path

    class FailedWrite:
        calls = 0

        def write(self, data):
            self.calls += 1
            if self.calls == 1:
                return original.write(data[:2])
            raise OSError("injected disk error")

        def __getattr__(self, name):
            return getattr(original, name)

    writer._handle = FailedWrite()
    try:
        with pytest.raises(OSError, match="injected disk error"):
            await writer.write(b"tail\n")
        with pytest.raises(OSError, match="injected disk error"):
            await writer.close()
        assert path.read_bytes() == b"head\nta"
        assert not list(tmp_path.rglob("*.tar.gz"))
        assert original.closed
    finally:
        original.close()


async def test_slow_partial_writes_preserve_duplicate_lines_and_durable_watermark(tmp_path):
    """超过200ms不取消或丢弃批次；短写仅补写剩余字节，持久水位包含本批。"""
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    await writer.write(b"head\n")
    original = writer._handle

    class PartialWrite:
        first = True

        def write(self, data):
            if self.first:
                self.first = False
                time.sleep(.25)
            return original.write(data[:3])

        def __getattr__(self, name):
            return getattr(original, name)

    writer._handle = PartialWrite()
    writer._last_sync -= 2
    await writer.write_many([(b"same\n", None), (b"same\n", None), (b"last\n", None)])
    expected = b"head\nsame\nsame\nlast\n"
    assert writer.active_path.read_bytes() == expected
    assert writer.snapshot()["bytesDurable"] == len(expected)
    await writer.close()
