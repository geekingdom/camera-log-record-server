"""内容读取必须与采集默认线程池隔离，取消不能提前释放执行槽。"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from camera_logs.logs.read_threads import ReadThreads


async def test_blocking_reads_do_not_occupy_default_write_executor():
    """占满读取线程后，模拟采集写盘仍可在唯一默认线程中立即执行。"""
    pool = ReadThreads(workers=1)
    loop = asyncio.get_running_loop()
    default = ThreadPoolExecutor(max_workers=1)
    previous = loop._default_executor
    loop.set_default_executor(default)
    entered, release = threading.Event(), threading.Event()

    def blocking_read():
        entered.set()
        release.wait(5)

    task = asyncio.create_task(pool.run(blocking_read))
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(.001)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "written"), .5) == "written"
    finally:
        release.set()
        await task
        await pool.close()
        loop._default_executor = previous
        default.shutdown(wait=True)


async def test_cancelled_read_holds_slot_until_thread_has_exited():
    """取消不可中断读时，后续请求不能与未退出的文件线程突破并发上限。"""
    pool = ReadThreads(workers=1)
    entered, release = threading.Event(), threading.Event()
    second_started = threading.Event()

    def blocking_read():
        entered.set()
        release.wait(5)

    first = asyncio.create_task(pool.run(blocking_read))
    second = None
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(.001)
        first.cancel()
        second = asyncio.create_task(pool.run(second_started.set))
        await asyncio.sleep(.02)
        assert not first.done()
        assert not second_started.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        assert second_started.is_set()
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await pool.close()
