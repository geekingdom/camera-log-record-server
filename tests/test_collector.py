"""采集器测试：验证接收时序、命令队列、超时和停止落盘语义。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncssh
import camera_logs.collection.collector as collector_module
import pytest
from camera_logs.collection.collector import Collector
from camera_logs.logs.storage import MAX_LOG_BYTES


class FakeConnection:
    def __init__(self):
        self.sent: list[bytes] = []
        self._received: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False
        self.reply_on_write: bytes | None = None

    async def read(self, _: int = 65536) -> bytes:
        item = await self._received.get()
        return item or b""

    async def write(self, data: bytes) -> None:
        self.sent.append(data)
        if self.reply_on_write:
            await self._received.put(self.reply_on_write)

    async def close(self) -> None:
        self.closed = True


class FailingReadConnection(FakeConnection):
    async def read(self, _: int = 65536) -> bytes:
        raise OSError("read failed")


class AsyncSshDisconnectedReadConnection(FakeConnection):
    """使用 AsyncSSH 的真实断线异常，避免测试替身误走 OSError 分支。"""
    async def read(self, _: int = 65536) -> bytes:
        raise asyncssh.ConnectionLost("synthetic SSH disconnect")


async def test_pending_batch_read_wait_ends_at_original_flush_deadline(tmp_path, monkeypatch):
    """批窗口结束前再次收到小包，下一次读等待不能再完整延长 100ms。"""
    collector = Collector({"id": "deadline", "runId": "run", "storageIdentity": "synthetic"}, tmp_path,
                          connection_factory=lambda _: FakeConnection())
    clock, timeouts = [0.0], []
    chunks = iter([(.06, b"first"), (.099, b"second"), (.1, b"")])

    async def read():
        clock[0], data = next(chunks)
        return data

    async def wait_for(awaitable, timeout):
        timeouts.append(timeout)
        return await awaitable

    collector._connection = SimpleNamespace(read=read)
    collector._accepting_commands = True
    collector._close_connection = AsyncMock()
    collector._writer.close = AsyncMock(return_value=None)
    collector._flush = AsyncMock()
    collector._publish_archives = AsyncMock()
    monkeypatch.setattr(collector_module, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: clock[0]),
        wait_for=wait_for, CancelledError=asyncio.CancelledError, current_task=asyncio.current_task))
    await collector._reader_loop()
    assert 0 < timeouts[2] <= .002


def test_collector_writes_received_chunks_in_exact_order_and_initializes_commands(tmp_path):
    async def scenario():
        connection = FakeConnection()
        received: list[bytes] = []
        collector = Collector(
            {
                "id": "task-a",
                "runId": "run-a",
                "storageIdentity": "testingdevice",
                "protocolType": "TELNET_SERIAL",
                "initialCommands": [{"command": "prepare"}, {"command": "start", "newline": "\r\n"}],
                "scheduledCommands": [],
            },
            tmp_path,
            connection_factory=lambda _: connection,
            on_log=lambda chunk: received.append(chunk.data),
        )
        await collector.start()
        await connection._received.put(b"a")
        await connection._received.put(b"b\n")
        await connection._received.put(None)
        await collector.wait_closed()
        return connection, received, collector

    connection, received, collector = asyncio.run(scenario())
    assert connection.sent == [b"prepare\n", b"start\r\n"]
    assert b"".join(received).endswith(b"ab\n")
    assert b"".join(received).count(b"[") == 1
    assert collector.session_id
    assert connection.closed


def test_collector_uses_incremental_pending_byte_count_for_small_packets(tmp_path, monkeypatch):
    """大量小分包不应在每次接收时遍历整个待刷写队列计算总长度。"""
    def forbidden_sum(*args, **kwargs):
        raise AssertionError("reader loop must not aggregate pending chunks with sum")

    monkeypatch.setattr(collector_module, "sum", forbidden_sum, raising=False)

    async def scenario():
        connection = FakeConnection()
        received: list[bytes] = []
        collector = Collector(
            {"id": "task-small", "runId": "run-small", "storageIdentity": "testingdevice",
             "protocolType": "TELNET_SERIAL", "initialCommands": [], "scheduledCommands": []},
            tmp_path, connection_factory=lambda _: connection, on_log=lambda chunk: received.append(chunk.data),
        )
        await collector.start()
        for _ in range(2_000):
            await connection._received.put(b"x")
        await connection._received.put(None)
        await collector.wait_closed()
        return received

    received = asyncio.run(scenario())
    assert b"".join(received).endswith(b"x" * 2_000)


def test_manual_command_is_serialized_after_initialization_and_never_replayed(tmp_path):
    async def scenario():
        connection = FakeConnection()
        collector = Collector(
            {"id": "task-a", "runId": "run-a", "storageIdentity": "testingdevice", "protocolType": "TELNET_SERIAL", "initialCommands": []},
            tmp_path,
            connection_factory=lambda _: connection,
        )
        await collector.start()
        command_id = await collector.enqueue_manual("show status")
        await collector.wait_for_command(command_id)
        await collector.stop()
        return connection, collector.command_status(command_id)

    sent, status = asyncio.run(scenario())
    assert sent.sent == [b"show status\n"]
    assert status == "SENT"


def test_manual_command_waits_for_its_prompt_within_serial_queue(tmp_path):
    async def scenario():
        connection = FakeConnection()
        connection.reply_on_write = b"manual-ready>"
        collector = Collector(
            {"id": "task-a", "runId": "run-a", "storageIdentity": "testingdevice", "initialCommands": []},
            tmp_path,
            connection_factory=lambda _: connection,
        )
        await collector.start()
        command_id = await collector.enqueue_manual("inspect", prompt="manual-ready>", timeout_seconds=1)
        await collector.stop()
        return command_id, connection.sent

    command_id, sent = asyncio.run(scenario())
    assert command_id
    assert sent == [b"inspect\n"]


def test_initial_command_prompt_is_read_by_live_reader_before_collecting(tmp_path):
    async def scenario():
        connection = FakeConnection()
        connection.reply_on_write = b"device-ready>"
        collector = Collector(
            {
                "id": "task-a",
                "runId": "run-a",
                "storageIdentity": "testingdevice",
                "initialCommands": [{"command": "configure", "prompt": "device-ready>"}],
            },
            tmp_path,
            connection_factory=lambda _: connection,
        )
        await asyncio.wait_for(collector.start(), timeout=1)
        await collector.stop()
        return connection.sent

    assert asyncio.run(scenario()) == [b"configure\n"]


def test_stopping_before_scheduled_send_does_not_consume_durable_budget(tmp_path):
    async def scenario():
        connection = FakeConnection()
        reserved = 0

        async def reserve(*_args):
            nonlocal reserved
            reserved += 1
            return True

        collector = Collector(
            {
                "id": "task-a", "runId": "run-a", "storageIdentity": "testingdevice", "initialCommands": [],
                "scheduledCommands": [{"id": "periodic", "command": "status", "totalExecutions": 1, "intervalSeconds": 10}],
            },
            tmp_path,
            connection_factory=lambda _: connection,
            reserve_execution=reserve,
        )
        await collector.start()
        await collector.stop()
        return reserved, connection.sent

    reserved, sent = asyncio.run(scenario())
    assert reserved == 0
    assert sent == []


def test_idle_timeout_closes_connection_and_reports_without_writing_commands(tmp_path):
    async def scenario():
        connection = FakeConnection()
        states = []
        collector = Collector(
            {"id": "task-a", "runId": "run-a", "storageIdentity": "testingdevice", "initialCommands": [], "logIdleTimeoutSeconds": .02},
            tmp_path, connection_factory=lambda _: connection, on_state=lambda state, _details: states.append(state),
        )
        await collector.start()
        await asyncio.wait_for(collector.wait_closed(), .5)
        return states, connection.closed, connection.sent
    states, closed, sent = asyncio.run(scenario())
    assert "IDLE_TIMEOUT" in states
    assert closed and sent == []


def test_stop_waits_for_reader_final_flush_and_archive(tmp_path):
    async def scenario():
        connection = FakeConnection()
        archives = []
        collector = Collector(
            {"id": "task-a", "runId": "run-a", "storageIdentity": "testingdevice", "initialCommands": []}, tmp_path,
            connection_factory=lambda _: connection, on_archive=archives.append,
        )
        await collector.start()
        await connection._received.put(b"must-persist")
        await asyncio.sleep(0)
        await collector.stop()
        return archives

    archives = asyncio.run(scenario())
    assert len(archives) == 1
    assert archives[0].raw_size > len(b"must-persist")


def test_reader_failure_still_unblocks_wait_closed_and_releases_connection(tmp_path):
    async def scenario():
        connection = FailingReadConnection()
        states = []
        collector = Collector(
            {"id": "task-a", "runId": "run-a", "storageIdentity": "testingdevice", "initialCommands": []}, tmp_path,
            connection_factory=lambda _: connection, on_state=lambda state, _details: states.append(state),
        )
        await collector.start()
        await asyncio.wait_for(collector.wait_closed(), .5)
        return connection.closed, states

    closed, states = asyncio.run(scenario())
    assert closed
    assert "READ_ERROR" in states
    assert "CLOSED" in states


def test_stop_allows_reconnect_after_network_reader_error(tmp_path):
    async def scenario():
        connection = FailingReadConnection()
        collector = Collector(
            {"id": "task-a", "runId": "run-a", "storageIdentity": "testingdevice", "initialCommands": []}, tmp_path,
            connection_factory=lambda _: connection,
        )
        await collector.start()
        await collector.wait_closed()
        await collector.stop()

    asyncio.run(scenario())


def test_stop_does_not_rethrow_asyncssh_disconnect_after_reader_closed(tmp_path):
    """真实 SSH 读断线已关闭传输后是可重连会话结束，不能粘成停止错误。"""
    async def scenario():
        connection = AsyncSshDisconnectedReadConnection()
        states = []
        collector = Collector(
            {"id": "task-a", "runId": "run-a", "storageIdentity": "testingdevice", "initialCommands": []}, tmp_path,
            connection_factory=lambda _: connection, on_state=lambda state, _details: states.append(state),
        )
        await collector.start()
        await asyncio.wait_for(collector.wait_closed(), .5)
        await collector.stop()
        return connection.closed, states

    closed, states = asyncio.run(scenario())
    assert closed
    assert "READ_ERROR" in states and "CLOSED" in states


def test_storage_failure_is_not_retried_during_stop(tmp_path):
    async def scenario():
        connection = FakeConnection()
        collector = Collector(
            {"id": "task-a", "runId": "run-a", "storageIdentity": "testingdevice", "initialCommands": []}, tmp_path,
            connection_factory=lambda _: connection,
        )
        attempts = 0

        async def fail_once(_pending):
            nonlocal attempts
            attempts += 1
            raise OSError("storage failed")

        collector._flush = fail_once
        await collector.start()
        await connection._received.put(b"x" * (256 * 1024))
        await asyncio.wait_for(collector.wait_closed(), .5)
        with pytest.raises(OSError, match="storage failed"):
            await collector.stop()
        return attempts

    assert asyncio.run(scenario()) == 1


def test_log_prefixes_cross_chunk_lines_empty_lines_and_duplicate_content(tmp_path):
    async def scenario():
        connection = FakeConnection()
        chunks = []
        collector = Collector(
            {"id": "task-a", "runId": "run-a", "storageIdentity": "testingdevice", "initialCommands": []}, tmp_path,
            connection_factory=lambda _: connection, on_log=lambda chunk: chunks.append(chunk.data),
        )
        await collector.start()
        await connection._received.put(b"same")
        await connection._received.put(b"\r\n\n")
        await connection._received.put(b"same\n")
        await connection._received.put(None)
        await collector.wait_closed()
        return b"".join(chunks)

    output = asyncio.run(scenario())
    lines = output.splitlines(keepends=True)
    assert len(lines) == 3
    assert all(line.startswith(b"[") for line in lines)
    assert lines[0].endswith(b"same\r\n")
    assert lines[1].endswith(b"\n")
    assert lines[2].endswith(b"same\n")


def test_flush_publishes_every_piece_when_one_prefixed_chunk_crosses_10_mib(tmp_path):
    """写入器拆分大块后，采集器必须按 source offset 发布完整字节流。"""
    async def scenario():
        chunks = []
        collector = Collector(
            {"id": "task", "runId": "run", "storageIdentity": "device", "initialCommands": []}, tmp_path,
            connection_factory=lambda _: FakeConnection(), on_log=lambda chunk: chunks.append(chunk),
        )
        data = b"[2026-09-08 09:00:00] partial-line-" + b"x" * (MAX_LOG_BYTES + 31)
        await collector._flush([(data, datetime.now(UTC))])
        await collector._writer.close()
        return data, chunks

    data, chunks = asyncio.run(scenario())
    assert len(chunks) == 2
    assert b"".join(chunk.data for chunk in chunks) == data
    assert [chunk.offset for chunk in chunks] == [0, 0]
