"""归档句柄复用的容量、身份、并发、水位与故障回收约束。"""

import asyncio
import io
import os
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from camera_logs.logs import archive_readers
from camera_logs.logs.archive_access import read_limiter
from camera_logs.logs.archive_readers import ArchiveReaders
from camera_logs.logs.file_reads import FileReads
from camera_logs.logs.source import open_log_source


def archive(path, data=b"abcdefghijklmnopqrst"):
    """创建真实压缩包，原子替换等测试可直接检查已打开文件的资源状态。"""
    with tarfile.open(path, "w:gz") as output:
        info = tarfile.TarInfo("part.log")
        info.size = len(data)
        output.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture
def opened(monkeypatch):
    """只跟踪真实流的打开与关闭，不替代解压、文件版本或正文读取。"""
    streams = []

    @contextmanager
    def track(*args):
        with open_log_source(*args) as source:
            streams.append(source.stream)
            yield source

    monkeypatch.setattr(archive_readers, "open_log_source", track)
    return streams


def test_capacity_and_expiry_close_real_handles(tmp_path, monkeypatch, opened):
    clock = [10.0]
    monkeypatch.setattr(archive_readers, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    pool = ArchiveReaders(capacity=1, idle_seconds=30)
    try:
        for name in ("a", "b"):
            assert pool.read(name, archive(tmp_path / f"{name}.tar.gz"), "part.log", 0, 3, 20) == b"abc"
        assert opened[0].closed and not opened[1].closed
        clock[0] = 41
        pool.prune()
        assert all(stream.closed for stream in opened)
    finally:
        pool.close()


@pytest.mark.parametrize("in_place", [False, True])
def test_replacement_never_reuses_previous_archive_inode(tmp_path, opened, in_place):
    path = archive(tmp_path / "hour.tar.gz")
    pool = ArchiveReaders()
    try:
        assert pool.read("file", path, "part.log", 0, 3, 20) == b"abc"
        replacement = archive(tmp_path / "replacement.tar.gz", b"ZYXWVUTSRQPONMLKJIHG")
        if in_place:
            path.write_bytes(replacement.read_bytes())
        else:
            os.replace(replacement, path)
        assert pool.read("file", path, "part.log", 3, 3, 20) == b"WVU"
        assert opened[0].closed
    finally:
        pool.close()
    assert all(stream.closed for stream in opened)


def test_archive_replaced_between_stat_and_open_is_not_cached_as_old_version(tmp_path, monkeypatch, opened):
    """路径预检与打开之间发生替换时，不能把新文件句柄登记为旧 inode 的缓存。"""
    path = archive(tmp_path / "hour.tar.gz")
    replacement = archive(tmp_path / "replacement.tar.gz", b"ZYXWVUTSRQPONMLKJIHG")
    original_open = archive_readers.open_log_source

    @contextmanager
    def race(*args):
        if replacement.exists():
            os.replace(replacement, path)
        with original_open(*args) as source:
            yield source

    monkeypatch.setattr(archive_readers, "open_log_source", race)
    pool = ArchiveReaders()
    try:
        assert pool.read("file", path, "part.log", 0, 3, 20) == b"ZYX"
        assert opened[0].closed
        assert pool.read("file", path, "part.log", 3, 3, 20) == b"WVU"
    finally:
        pool.close()


def test_appending_raw_logs_are_never_cached_at_a_stale_size(tmp_path, opened):
    path = tmp_path / "current.log"
    path.write_bytes(b"abc")
    pool = ArchiveReaders()
    try:
        assert pool.read("file", path, None, 0, 3, 3) == b"abc"
        assert opened[0].closed
        with path.open("ab") as stream:
            stream.write(b"def")
        assert pool.read("file", path, None, 3, 3, 6) == b"def"
    finally:
        pool.close()


def test_cached_stream_does_not_override_new_frozen_watermark(tmp_path, opened):
    path = archive(tmp_path / "hour.tar.gz")
    pool = ArchiveReaders()
    try:
        assert pool.read("file", path, "part.log", 0, 3, 20) == b"abc"
        assert pool.read("file", path, "part.log", 3, 10, 5) == b"de"
        assert pool.read("file", path, "part.log", 5, 10, 4) == b""
    finally:
        pool.close()
    assert all(stream.closed for stream in opened)


def test_changed_identity_cannot_borrow_existing_cursor(tmp_path, opened):
    path = archive(tmp_path / "hour.tar.gz")
    pool = ArchiveReaders()
    try:
        pool.read(("task-a", "run-a"), path, "part.log", 0, 3, 20)
        assert pool.read(("task-b", "run-b"), path, "part.log", 3, 3, 20) == b"def"
        assert len(opened) == 2
    finally:
        pool.close()


def test_failed_read_discards_cursor_and_preserves_original_exception(tmp_path, monkeypatch, opened):
    path = archive(tmp_path / "hour.tar.gz")
    pool = ArchiveReaders()
    pool.read("file", path, "part.log", 0, 3, 20)

    def fail(_size):
        raise OSError("injected read budget failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(read_limiter, "consume", fail)
            with pytest.raises(OSError, match="injected read budget failure"):
                pool.read("file", path, "part.log", 3, 3, 20)
        assert opened[0].closed
        assert pool.read("file", path, "part.log", 3, 3, 20) == b"def"
        assert len(opened) == 2
    finally:
        pool.close()


def test_concurrent_same_cursor_never_shares_in_use_stream(tmp_path, monkeypatch, opened):
    path = archive(tmp_path / "hour.tar.gz")
    pool = ArchiveReaders()
    pool.read("file", path, "part.log", 0, 4, 20)
    barrier = threading.Barrier(2)

    def overlap(size):
        if size == 3:
            barrier.wait(timeout=2)

    monkeypatch.setattr(read_limiter, "consume", overlap)
    try:
        with ThreadPoolExecutor(max_workers=2) as threads:
            futures = [threads.submit(pool.read, "file", path, "part.log", 4, 3, 20) for _ in range(2)]
            assert [future.result(timeout=3) for future in futures] == [b"efg", b"efg"]
        assert len(opened) == 2
    finally:
        pool.close()
    assert all(stream.closed for stream in opened)


async def test_idle_node_reclaims_cached_handle_without_another_request(tmp_path, opened):
    reads = FileReads(idle_seconds=.01)
    try:
        assert await reads.read("file", archive(tmp_path / "hour.tar.gz"), "part.log", 0, 3, 20) == b"abc"
        async with asyncio.timeout(1):
            while not opened[0].closed:
                await asyncio.sleep(.005)
    finally:
        await reads.close()
    assert reads._sweeper.done()
    with pytest.raises(RuntimeError, match="已关闭"):
        await reads.read("file", tmp_path / "hour.tar.gz", "part.log", 3, 3, 20)


async def test_sweeper_failure_is_logged_and_next_cycle_still_reclaims(tmp_path, monkeypatch, opened, caplog):
    """周期回收异常不能悄悄终止定期清理，下一周期仍应关闭过期句柄。"""
    reads = FileReads(idle_seconds=.01)
    await reads.read("file", archive(tmp_path / "hour.tar.gz"), "part.log", 0, 3, 20)
    original, calls = reads._sources.prune, []

    def fail_once():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("injected sweep failure")
        return original()

    monkeypatch.setattr(reads._sources, "prune", fail_once)
    try:
        async with asyncio.timeout(1):
            while not opened[0].closed:
                await asyncio.sleep(.005)
        assert len(calls) >= 2
        assert "归档空闲句柄回收失败" in caplog.text
    finally:
        await reads.close()


async def test_cancelled_read_and_shutdown_wait_for_thread_then_close_handles(tmp_path, monkeypatch, opened):
    """请求与关闭重复取消时，工作线程仍须先退出，随后释放其借用的归档句柄。"""
    reads = FileReads()
    path = archive(tmp_path / "hour.tar.gz")
    await reads.read("file", path, "part.log", 0, 3, 20)
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()

    def block(_size):
        entered.set()
        assert release.wait(3)

    shutdown = reads._threads._executor.shutdown

    def close_threads(*args, **kwargs):
        closing.set()
        return shutdown(*args, **kwargs)

    monkeypatch.setattr(read_limiter, "consume", block)
    monkeypatch.setattr(reads._threads._executor, "shutdown", close_threads)
    pending = asyncio.create_task(reads.read("file", path, "part.log", 3, 3, 20))
    close = None
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(.001)
        pending.cancel()
        close = asyncio.create_task(reads.close())
        async with asyncio.timeout(1):
            while not closing.is_set():
                await asyncio.sleep(.001)
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        assert not pending.done() and not opened[0].closed
        release.set()
        for task in (pending, close):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert all(stream.closed for stream in opened)
    finally:
        release.set()
        await asyncio.gather(pending, *([close] if close else []), return_exceptions=True)
        await reads.close()
