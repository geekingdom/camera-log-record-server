"""平台日志分片大小在节点缓存、采集换片和下载重组中的回归。"""

import asyncio
import io
import tarfile
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from camera_logs.logs.hour_download import write_hour_archive
from camera_logs.logs.segment_size import (
    LogSegmentSizeCache,
    log_segment_bytes,
)
from camera_logs.logs.storage import HourlyWriter


def test_segment_size_validation_defaults_legacy_or_invalid_values():
    """旧配置、布尔值、越界值和非法档位均只能回退为十 MiB。"""
    mib = 1024 * 1024
    assert log_segment_bytes(None) == 10 * mib
    assert log_segment_bytes(True) == 10 * mib
    assert log_segment_bytes(9) == 10 * mib
    assert log_segment_bytes(11) == 10 * mib
    assert log_segment_bytes(25) == 25 * mib
    assert log_segment_bytes(510) == 510 * mib
    assert log_segment_bytes(512) == 512 * mib
    assert log_segment_bytes(511) == 10 * mib
    assert log_segment_bytes(513) == 10 * mib


async def test_shared_cache_refresh_keeps_previous_value_when_database_fails():
    """刷新失败不让节点回退错误值，下一次新分片继续使用最后有效配置。"""
    collection = SimpleNamespace(find_one=lambda _query: None)
    repo = SimpleNamespace(db=SimpleNamespace(platform_settings=collection))
    cache = LogSegmentSizeCache()

    async def configured(_query):
        return {"logSegmentMiB": 25}

    collection.find_one = configured
    assert await cache.refresh(repo) == 25 * 1024 * 1024
    assert cache.current() == 25 * 1024 * 1024

    async def unavailable(_query):
        raise OSError("database unavailable")

    collection.find_one = unavailable
    try:
        await cache.refresh(repo)
    except OSError:
        pass
    assert cache.current() == 25 * 1024 * 1024


async def test_shared_cache_loop_cancellation_stops_without_background_work():
    """节点关闭取消刷新循环后，循环不得吞掉取消或遗留下一次数据库读取。"""
    refreshed = asyncio.Event()
    calls = 0

    async def configured(_query):
        nonlocal calls
        calls += 1
        refreshed.set()
        return {"logSegmentMiB": 25}

    repo = SimpleNamespace(db=SimpleNamespace(platform_settings=SimpleNamespace(find_one=configured)))
    cache = LogSegmentSizeCache()
    task = asyncio.create_task(cache.loop(repo, interval_seconds=3600))
    await asyncio.wait_for(refreshed.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert task.done() and task.cancelled()
    assert calls == 1


async def test_running_writer_applies_new_limit_only_after_opening_next_fragment(tmp_path):
    """收紧上限不影响已打开分片；下一片读取缓存值且不会产生负剩余容量。"""
    limits = iter((8, 4, 4))
    writer = HourlyWriter(
        "task", "run", "session", tmp_path, storage_identity="device",
        segment_bytes=lambda: next(limits),
    )
    await writer.write(b"abcdefghijklm", received_at=datetime(2026, 10, 23, tzinfo=UTC))
    archive = await writer.close()
    with tarfile.open(archive.path, "r:gz") as bundle:
        assert [item.size for item in bundle if item.isfile()] == [8, 4, 1]


def test_download_rebuild_uses_frozen_platform_limit(tmp_path):
    """下载重组接收调用方固定的配置上限，不再将历史成员硬拆为十 MiB。"""
    source, output = tmp_path / "source.tar.gz", tmp_path / "output.tar.gz"
    with tarfile.open(source, "w:gz") as bundle:
        info = tarfile.TarInfo("source.log")
        info.size = 13
        bundle.addfile(info, io.BytesIO(b"abcdefghijklm"))
    document = {"id": "file", "bytes": 13, "archiveMember": "source.log"}
    write_hour_archive(output, [(document, document, source, True)], max_member_bytes=4)
    with tarfile.open(output, "r:gz") as bundle:
        assert [item.size for item in bundle if item.isfile()] == [4, 4, 4, 1]
