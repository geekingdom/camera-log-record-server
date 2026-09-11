"""命令派发取消竞态：发送器已取队列但尚未进入预算或 socket 写入时应安全跳过。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection.collector import Collector


class Connection:
    """通过异步队列模拟设备连接，不连接真实设备。"""

    def __init__(self):
        self.received = asyncio.Queue()
        self.sent = []

    async def read(self):
        return await self.received.get()

    async def write(self, data):
        self.sent.append(data)

    async def close(self):
        await self.received.put(b"")


def collector_for(tmp_path, connection):
    """创建独立采集会话与临时日志根目录。"""
    return Collector({"id": "cancel-dispatch", "runId": "run", "storageIdentity": "synthetic"},
                     tmp_path, connection_factory=lambda _: connection)


async def test_cancel_while_session_guard_waits_skips_budget_and_socket_write(tmp_path):
    """调用方在守卫等待中取消时，命令尚未发送，不得预留预算或写入设备。"""
    connection = Connection()
    collector = collector_for(tmp_path, connection)
    guard_entered, release_guard = asyncio.Event(), asyncio.Event()
    reserve = AsyncMock(return_value=True)

    async def guard():
        guard_entered.set()
        await release_guard.wait()

    await collector.start()
    cancelled = asyncio.create_task(collector._enqueue(
        "cancelled-before-send", "\n", priority=1, before_send=reserve, session_guard=guard,
    ))
    try:
        await asyncio.wait_for(guard_entered.wait(), .5)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        release_guard.set()
        await asyncio.sleep(0)
        await collector._enqueue("barrier", "\n", priority=1)
        reserve.assert_not_awaited()
        assert connection.sent == [b"barrier\n"]
    finally:
        release_guard.set()
        await collector.stop()
        await asyncio.gather(cancelled, return_exceptions=True)
