"""命令派发边界：提示符只属于当前命令，优先级不能改变正在执行的命令。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection.collector import Collector


class Connection:
    """通过异步队列模拟独立设备，不建立真实SSH或Telnet连接。"""

    def __init__(self):
        self.received = asyncio.Queue()
        self.sent = []
        self.responses = {}

    async def read(self):
        return await self.received.get()

    async def write(self, data):
        self.sent.append(data)
        for chunk in self.responses.get(data, []):
            await self.received.put(chunk)

    async def close(self):
        await self.received.put(b"")


def collector_for(tmp_path, connection):
    """每个测试独立会话与存储目录，确保测试收尾不影响设备日志。"""
    return Collector({"id": "dispatch", "runId": "run", "storageIdentity": "synthetic"},
                     tmp_path, connection_factory=lambda _: connection)


async def test_previous_prompt_tail_cannot_complete_next_command(tmp_path):
    """旧命令的D>加上新收到的new不能伪造当前命令的完整D>new提示符。"""
    connection = Connection()
    connection.responses = {b"first\n": [b"OLD>"], b"second\n": [b"new"]}
    collector = collector_for(tmp_path, connection)
    await collector.start()
    try:
        await collector.enqueue_manual("first", prompt="OLD>", timeout_seconds=.1)
        with pytest.raises(TimeoutError):
            await collector.enqueue_manual("second", prompt="D>new", timeout_seconds=.05)
    finally:
        await collector.stop()


async def test_current_prompt_can_span_multiple_receive_chunks(tmp_path):
    """同一命令内的跨分包提示符应保持有效，不能因缓冲隔离而丢失。"""
    connection = Connection()
    connection.responses = {b"inspect\n": [b"noise rea", b"dy", b"> "]}
    collector = collector_for(tmp_path, connection)
    await collector.start()
    try:
        identifier = await collector.enqueue_manual("inspect", prompt="ready>", timeout_seconds=.2)
        assert collector.command_status(identifier) == "SENT"
        assert connection.sent == [b"inspect\n"]
    finally:
        await collector.stop()


async def test_manual_priority_preserves_inflight_command_and_scheduled_fifo(tmp_path):
    """手动命令可越过未开始的定时命令，但不能插入当前命令或逆转同优先级顺序。"""
    connection = Connection()
    collector = collector_for(tmp_path, connection)
    entered, release = asyncio.Event(), asyncio.Event()

    async def guard():
        entered.set()
        await release.wait()

    await collector.start()
    pending = []
    try:
        pending.append(asyncio.create_task(collector.enqueue_manual("inflight", session_guard=guard)))
        await entered.wait()
        for command in ("scheduled-a", "scheduled-b"):
            pending.append(asyncio.create_task(collector._enqueue(command, "\n", priority=2)))
        pending.append(asyncio.create_task(collector.enqueue_manual("manual")))
        await asyncio.sleep(0)
        assert connection.sent == []
        release.set()
        await asyncio.wait_for(asyncio.gather(*pending), .5)
        assert connection.sent == [b"inflight\n", b"manual\n", b"scheduled-a\n", b"scheduled-b\n"]
    finally:
        release.set()
        await collector.stop()
        await asyncio.gather(*pending, return_exceptions=True)


async def test_cancelled_queued_command_does_not_reserve_or_write(tmp_path):
    """排队取消未进入发送阶段，不能占用定时预算，也不能在后续释放队列时补发。"""
    connection = Connection()
    collector = collector_for(tmp_path, connection)
    entered, release = asyncio.Event(), asyncio.Event()
    reserve = AsyncMock(return_value=True)

    async def guard():
        entered.set()
        await release.wait()

    await collector.start()
    active = asyncio.create_task(collector.enqueue_manual("inflight", session_guard=guard))
    queued = None
    try:
        await entered.wait()
        queued = asyncio.create_task(collector._enqueue("cancelled", "\n", priority=2, before_send=reserve))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await active
        await collector._enqueue("barrier", "\n", priority=2)
        reserve.assert_not_awaited()
        assert connection.sent == [b"inflight\n", b"barrier\n"]
    finally:
        release.set()
        await collector.stop()
        await asyncio.gather(active, *([queued] if queued else []), return_exceptions=True)


async def test_stop_releases_inflight_and_queued_command_waiters(tmp_path):
    """关闭当前连接时，等提示符及尚未发送的调用者都必须结束，不能留在队列等待。"""
    connection = Connection()
    collector = collector_for(tmp_path, connection)
    await collector.start()
    active = asyncio.create_task(collector.enqueue_manual("waiting", prompt="never-arrives", timeout_seconds=30))
    pending = []
    try:
        async with asyncio.timeout(.5):
            while not connection.sent:
                await asyncio.sleep(0)
        pending.append(asyncio.create_task(collector.enqueue_manual("unsent")))
        await asyncio.sleep(0)
        await asyncio.wait_for(collector.stop(), .5)
        results = await asyncio.wait_for(asyncio.gather(active, *pending, return_exceptions=True), .5)
        assert all(isinstance(result, ConnectionError) for result in results)
        assert connection.sent == [b"waiting\n"]
    finally:
        await collector.stop()
        await asyncio.gather(active, *pending, return_exceptions=True)
