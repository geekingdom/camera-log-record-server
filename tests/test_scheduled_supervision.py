"""定时后台错误必须可见并关闭原会话，不能静默丢失后续命令。"""

import asyncio
import logging

import pytest
from camera_logs.collection.collector import Collector
from test_collector import FakeConnection


async def test_failed_result_callback_closes_session_and_logs_identity(tmp_path, caplog):
    """设备只收到一次命令；不可恢复回写错误唤醒会话收尾并记录任务身份。"""
    connection = FakeConnection()

    async def failed_result(*_args):
        raise ValueError("injected invalid execution update")

    collector = Collector({"id": "task", "runId": "run", "storageIdentity": "synthetic",
        "initialCommands": [], "scheduledCommands": [{"id": "periodic", "command": "probe",
            "totalExecutions": 2, "intervalSeconds": .01}]}, tmp_path,
        connection_factory=lambda _: connection, update_execution=failed_result)
    caplog.set_level(logging.ERROR)
    await collector.start()
    try:
        await asyncio.wait_for(collector.wait_closed(), 1)
        assert connection.closed
        assert connection.sent == [b"probe\n"]
        assert "定时命令后台执行失败" in caplog.text
        assert "task=task" in caplog.text
        assert "command=periodic" in caplog.text
        with pytest.raises(ValueError, match="injected invalid"):
            await collector.stop()
    finally:
        await asyncio.gather(collector.stop(), return_exceptions=True)


async def test_unknown_scheduled_enqueue_failure_marks_unknown_and_closes_session(tmp_path):
    """写入通道报错时设备是否收到命令未知，必须收尾本会话而不能继续复用连接。"""
    connection = FakeConnection()
    updates = []

    async def record_update(*arguments):
        updates.append(arguments)

    collector = Collector({"id": "task", "runId": "run", "storageIdentity": "synthetic",
        "initialCommands": [], "scheduledCommands": [{"id": "periodic", "command": "probe",
            "totalExecutions": 1, "intervalSeconds": .01}]}, tmp_path,
        connection_factory=lambda _: connection, update_execution=record_update)

    async def uncertain_enqueue(*_args, **_kwargs):
        raise ConnectionError("socket write outcome unknown")

    collector._enqueue = uncertain_enqueue
    await collector.start()
    try:
        await asyncio.wait_for(collector.wait_closed(), 1)
        assert connection.closed
        assert [update[1] for update in updates] == ["UNKNOWN"]
        with pytest.raises(ConnectionError, match="outcome unknown"):
            await collector.stop()
    finally:
        await asyncio.gather(collector.stop(), return_exceptions=True)


async def test_unreserved_scheduled_write_then_runtime_error_is_unknown_and_closes_session(tmp_path):
    """无预算回调也会先授权再写 socket；写后任意异常不能误报为确定未发送。"""
    class WriteThenFailConnection(FakeConnection):
        async def write(self, data):
            await super().write(data)
            raise RuntimeError("write completed but caller failed")

    connection, updates = WriteThenFailConnection(), []

    async def record_update(*arguments):
        updates.append(arguments)

    collector = Collector({"id": "task", "runId": "run", "storageIdentity": "synthetic",
        "initialCommands": [], "scheduledCommands": [{"id": "periodic", "command": "probe",
            "totalExecutions": 1, "intervalSeconds": .01}]}, tmp_path,
        connection_factory=lambda _: connection, update_execution=record_update)
    await collector.start()
    try:
        await asyncio.wait_for(collector.wait_closed(), 1)
        assert connection.closed and connection.sent == [b"probe\n"]
        assert [update[1] for update in updates] == ["UNKNOWN"]
        with pytest.raises(RuntimeError, match="write completed"):
            await collector.stop()
    finally:
        await asyncio.gather(collector.stop(), return_exceptions=True)


async def test_unsent_scheduled_channel_block_is_recorded_without_closing_session(tmp_path):
    """未进入发送预留的通道阻塞应保留采集连接，并留下可审计的未发送事件。"""
    connection = FakeConnection()
    events = []
    collector = Collector({"id": "task", "runId": "run", "storageIdentity": "synthetic"}, tmp_path,
        connection_factory=lambda _: connection,
        on_state=lambda state, details: events.append((state, details)) or collector._closed.set())

    async def blocked_enqueue(*_args, **_kwargs):
        from camera_logs.collection.collector import CommandChannelBlocked
        raise CommandChannelBlocked("shell not confirmed")

    collector._enqueue = blocked_enqueue
    await collector._scheduled_loop(0, {"id": "periodic", "command": "probe",
                                         "totalExecutions": 1, "intervalSeconds": .001})

    assert not connection.closed
    assert [(state, details["commandId"], details["outcome"]) for state, details in events] == [
        ("SCHEDULED_COMMAND_UNSENT", "periodic", "FAILED"),
    ]


async def test_unsent_unexpected_enqueue_failure_records_event_and_continues(tmp_path):
    """发送前未知异常留下失败事件，后续周期仍使用同一连接正常发送。"""
    connection, events, updates = FakeConnection(), [], []
    completed = asyncio.Event()

    async def update(_identifier, status, _detail):
        updates.append(status)
        if status == "SENT":
            completed.set()

    collector = Collector({"id": "task", "runId": "run", "storageIdentity": "synthetic",
        "initialCommands": [], "scheduledCommands": [{"id": "periodic", "command": "probe",
            "totalExecutions": 2, "intervalSeconds": .01}]}, tmp_path,
        connection_factory=lambda _: connection, update_execution=update,
        on_state=lambda state, detail: events.append((state, detail)))
    original, calls = collector._enqueue, 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("synthetic enqueue failure")
        return await original(*args, **kwargs)

    collector._enqueue = fail_once
    await collector.start()
    try:
        await asyncio.wait_for(completed.wait(), 1)
        assert not connection.closed and connection.sent == [b"probe\n"]
        assert updates == ["FAILED", "SENT"]
        failures = [detail for state, detail in events if state == "SCHEDULED_COMMAND_UNSENT"]
        assert len(failures) == 1 and failures[0]["errorType"] == "ValueError"
    finally:
        await collector.stop()
