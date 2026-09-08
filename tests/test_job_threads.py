"""作业线程取消必须等实际收尾，重复取消和后台异常均不能跳过该边界。"""

import asyncio
import threading

import pytest
from camera_logs.logs.job_threads import job_thread


@pytest.mark.parametrize("failure", [False, True])
async def test_repeated_cancellation_joins_worker_before_propagating(failure):
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    stop = threading.Event()

    def operation():
        entered.set()
        try:
            release.wait()
            if failure:
                raise OSError("injected background failure")
            return 42
        finally:
            exited.set()

    task = asyncio.create_task(job_thread(operation, stop=stop))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert stop.is_set() and not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not exited.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert exited.is_set()


async def test_normal_thread_return_and_failure_propagate():
    assert await job_thread(lambda: 42) == 42

    def fail():
        raise OSError("injected")

    with pytest.raises(OSError, match="injected"):
        await job_thread(fail)
