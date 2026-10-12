"""采集写盘执行器的有界提交、取消收尾和动态并发回归。"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from camera_logs.logs import write_io


async def test_cancelled_write_waits_for_thread_exit_before_releasing_capacity(monkeypatch):
    """取消不能让第二次落盘越过仍在执行的首个文件操作。"""
    await write_io.shutdown()
    write_io.configure(1)
    entered, release, second_started = (threading.Event() for _ in range(3))

    def first():
        entered.set()
        release.wait(2)

    first_task = asyncio.create_task(write_io.write_thread(first))
    assert await asyncio.to_thread(entered.wait, 1)
    first_task.cancel()
    second = asyncio.create_task(write_io.write_thread(second_started.set))
    await asyncio.sleep(.02)
    assert not first_task.done() and not second_started.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    await second
    assert second_started.is_set()
    await write_io.shutdown()


async def test_dynamic_shrink_waits_for_existing_writes_then_limits_new_submissions():
    """缩容不取消既有写入，排空后新写入严格遵循较低并发上限。"""
    await write_io.shutdown()
    write_io.configure(2)
    entered, release, third_started = (threading.Event() for _ in range(3))

    def blocking():
        entered.set()
        release.wait(2)

    first = asyncio.create_task(write_io.write_thread(blocking))
    second = asyncio.create_task(write_io.write_thread(blocking))
    assert await asyncio.to_thread(entered.wait, 1)
    while write_io.snapshot()["active"] != 2:
        await asyncio.sleep(.001)
    write_io.configure(1)
    third = asyncio.create_task(write_io.write_thread(third_started.set))
    await asyncio.sleep(.02)
    assert not third_started.is_set()
    release.set()
    await asyncio.gather(first, second)
    await third
    assert third_started.is_set()
    assert write_io.snapshot()["concurrency"] == 1
    await write_io.shutdown()


def test_gate_can_be_reused_after_a_waiting_event_loop_closes():
    """门控满载等待过后，下一次应用事件循环仍能提交独立写入。"""
    entered, release = threading.Event(), threading.Event()

    def blocked_write():
        entered.set()
        release.wait(3)

    async def first_lifecycle():
        await write_io.shutdown()
        write_io.configure(1)
        first = asyncio.create_task(write_io.write_thread(blocked_write))
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(write_io.write_thread(lambda: None))
        await asyncio.sleep(0)
        assert write_io.snapshot()["waiting"] == 1
        release.set()
        await asyncio.gather(first, second)
        await write_io.shutdown()

    async def second_lifecycle():
        write_io.configure(1)
        assert await write_io.write_thread(lambda: "ready") == "ready"
        await write_io.shutdown()

    asyncio.run(first_lifecycle())
    asyncio.run(second_lifecycle())


async def test_cancelled_shutdown_still_drains_the_executor_before_resetting():
    """关闭请求被取消也必须等在途写盘线程退出，不能遗留可复用的旧线程池。"""
    await write_io.shutdown()
    write_io.configure(1)
    entered, release = threading.Event(), threading.Event()

    def blocked_write():
        entered.set()
        release.wait(3)

    writing = asyncio.create_task(write_io.write_thread(blocked_write))
    assert await asyncio.to_thread(entered.wait, 2)
    closing = asyncio.create_task(write_io.shutdown())
    await asyncio.sleep(0)
    closing.cancel()
    try:
        await asyncio.sleep(.02)
        assert not closing.done()
    finally:
        release.set()
    await writing
    with pytest.raises(asyncio.CancelledError):
        await closing
    write_io.configure(1)
    assert await write_io.write_thread(lambda: "new lifecycle") == "new lifecycle"
    await write_io.shutdown()


async def test_write_executor_isolated_from_a_blocked_default_executor(tmp_path):
    """搜索占满默认线程池时，采集专用池仍能完成真实文件写入。"""
    await write_io.shutdown()
    write_io.configure(1)
    loop = asyncio.get_running_loop()
    default_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blocked-search")
    replacement_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="restored-default")
    search_started, write_done = asyncio.Event(), asyncio.Event()
    release_search = threading.Event()
    default_path, write_path = tmp_path / "default.log", tmp_path / "isolated.log"

    def blocked_search():
        loop.call_soon_threadsafe(search_started.set)
        release_search.wait(3)

    def isolated_write():
        write_path.write_bytes(b"isolated write\n")
        loop.call_soon_threadsafe(write_done.set)

    loop.set_default_executor(default_executor)
    blocked = asyncio.create_task(asyncio.to_thread(blocked_search))
    default_write = None
    try:
        await asyncio.wait_for(search_started.wait(), 1)
        default_write = asyncio.create_task(asyncio.to_thread(default_path.write_bytes, b"default write\n"))
        isolated = asyncio.create_task(write_io.write_thread(isolated_write))
        await asyncio.wait_for(write_done.wait(), 1)
        assert write_path.read_bytes() == b"isolated write\n"
        assert not default_write.done()
        release_search.set()
        await asyncio.gather(blocked, default_write, isolated)
        assert default_path.read_bytes() == b"default write\n"
    finally:
        release_search.set()
        await asyncio.gather(blocked, *(task for task in (default_write,) if task is not None), return_exceptions=True)
        loop.set_default_executor(replacement_executor)
        default_executor.shutdown(wait=True)
        await write_io.shutdown()


async def test_io_tracker_starts_only_after_real_io_slot_and_ends_on_failure():
    """真实磁盘指标不受门控排队影响，物理失败结束后不能遗留永久pending。"""
    from camera_logs.common.write_metrics import WriteLatency
    await write_io.shutdown()
    write_io.configure(1)
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        release.wait(3)
    first = asyncio.create_task(write_io.write_thread(blocked))
    assert await asyncio.to_thread(entered.wait, 1)
    tracker = WriteLatency()
    def failure():
        raise OSError("synthetic write failure")
    async def measured():
        with write_io.measure_io(tracker):
            await write_io.write_thread(failure)
    second = asyncio.create_task(measured())
    try:
        await asyncio.sleep(.02)
        assert tracker.snapshot()["pendingMs"] == 0
        assert not second.done()
    finally:
        release.set()
    await first
    with pytest.raises(OSError):
        await second
    assert tracker.snapshot()["samples"] == 0
    assert tracker.snapshot()["pendingMs"] == 0
    await write_io.shutdown()
