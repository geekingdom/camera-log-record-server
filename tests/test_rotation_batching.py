"""分片及小时边界仍按文件批量写入，避免小块串行调度放大慢盘等待。"""

from datetime import UTC, datetime, timedelta

import pytest
from camera_logs.logs import storage
from camera_logs.logs.storage import HourlyWriter


@pytest.mark.parametrize("boundary", ["hour", "capacity", "rollback"])
async def test_boundary_preserves_batching_and_source_offsets(tmp_path, monkeypatch, boundary):
    """跨边界的120个小块最多各分卷追加一次，正文/序号/源偏移均不改变。"""
    monkeypatch.setattr(storage, "schedule_pending_archive", lambda *_args, **_kwargs: None)
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    stamp = datetime(2026, 10, 30, 7, tzinfo=UTC)
    await writer.write(b"head\n", received_at=stamp)
    if boundary == "capacity":
        writer._segment_max_bytes = 243
        monkeypatch.setattr(storage, "MAX_LOG_BYTES", 243)
    appends = []
    original = storage.write_thread

    async def measured(function, *args, **kwargs):
        if function.__name__ in {"_append_sync", "_append_batch_sync"}:
            appends.append(function.__name__)
        return await original(function, *args, **kwargs)

    monkeypatch.setattr(storage, "write_thread", measured)
    next_stamp = stamp + timedelta(hours=1) if boundary == "hour" else stamp - timedelta(seconds=1)
    chunks = [(b"same\n", stamp if boundary == "capacity" or i < 48 else next_stamp) for i in range(120)]
    positions = await writer.write_many(chunks)
    await writer.close(defer=True)
    assert len(appends) <= 4, f"边界批次退化为逐小块写入：{len(appends)}次"
    assert [item.sequence for item in positions] == list(range(2, 2 + len(positions)))
    reconstructed = []
    for position in positions:
        expected = chunks[position.source_index][0][position.source_offset:position.source_offset + position.length]
        with position.path.open("rb") as handle:
            handle.seek(position.offset)
            assert handle.read(position.length) == expected
        reconstructed.append(expected)
    assert b"".join(reconstructed) == b"".join(data for data, _ in chunks)


async def test_hour_switch_isolated_from_blocked_default_executor(tmp_path, monkeypatch):
    """后台默认线程池全部被占用时，小时封存清单仍可完成并继续当前小时写入。"""
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(storage, "schedule_pending_archive", lambda *_args, **_kwargs: None)
    loop = asyncio.get_running_loop()
    original_executor = loop._default_executor
    blocked_pool = ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    loop.set_default_executor(blocked_pool)
    blocker = loop.run_in_executor(None, release.wait, 5)
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    stamp = datetime(2026, 10, 30, 7, tzinfo=UTC)
    writing = None
    try:
        await writer.write(b"first\n", received_at=stamp)
        writing = asyncio.create_task(writer.write(b"second\n", received_at=stamp + timedelta(hours=1)))
        done, _ = await asyncio.wait({writing}, timeout=.5)
        assert writing in done, "小时封存被后台默认线程池阻塞"
        writing.result()
        assert writer.active_path.read_bytes() == b"second\n"
    finally:
        release.set()
        await blocker
        if writing is not None:
            await writing
        await writer.close(defer=True)
        loop._default_executor = original_executor
        blocked_pool.shutdown(wait=True)
