"""关闭确认只能在适配器成功返回后发布，并发和取消不得误报连接释放。"""

import asyncio

import pytest
from camera_logs.collection.collector import Collector


def collector_for(tmp_path, connection):
    """只构造关闭路径所需实例，不启动接收、日志或真实网络连接。"""
    collector = Collector({"id": "task", "runId": "run", "storageIdentity": "synthetic"},
                          tmp_path, connection_factory=lambda _: connection)
    collector._connection = connection
    return collector


async def test_failed_close_is_not_confirmed_and_can_retry(tmp_path):
    """首次关闭失败不能让后续关闭静默跳过。"""
    class Connection:
        calls = 0

        async def close(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError("injected close failure")

    connection = Connection()
    collector = collector_for(tmp_path, connection)
    with pytest.raises(OSError):
        await collector._close_connection()
    assert not collector._connection_closed
    assert not collector._accepting_commands
    await collector._close_connection()
    assert collector._connection_closed
    assert connection.calls == 2


async def test_concurrent_close_waits_for_actual_confirmation(tmp_path):
    """两个收尾调用共享串行确认，第二个不能在底层还未关闭时提前完成。"""
    entered, release = asyncio.Event(), asyncio.Event()

    class Connection:
        calls = 0

        async def close(self):
            self.calls += 1
            entered.set()
            await release.wait()

    connection = Connection()
    collector = collector_for(tmp_path, connection)
    first = asyncio.create_task(collector._close_connection())
    await entered.wait()
    second = asyncio.create_task(collector._close_connection())
    try:
        await asyncio.sleep(0)
        assert not collector._connection_closed
        assert not second.done()
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert collector._connection_closed and connection.calls == 1


async def test_cancelled_close_can_be_retried(tmp_path):
    """取消发生在等待关闭确认时，后续仍能重新确认连接释放。"""
    entered = asyncio.Event()

    class Connection:
        calls = 0

        async def close(self):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await asyncio.Future()

    connection = Connection()
    collector = collector_for(tmp_path, connection)
    closing = asyncio.create_task(collector._close_connection())
    await entered.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not collector._connection_closed
    await collector._close_connection()
    assert collector._connection_closed and connection.calls == 2
