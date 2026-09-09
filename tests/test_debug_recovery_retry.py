"""debug 恢复暂未确认不能永久终止普通命令及未发送的定时预算。"""

import asyncio

import pytest
from test_debug_collector import PASSWORD, PSH_LS, Collector, FakeConnection, device_challenge


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["initial", "manual", "scheduled"])
async def test_later_ordinary_command_recovers_after_failed_debug(tmp_path, kind):
    """第一次 Ctrl-C 无确认，下一命令重探成功后发送，不重复 debug 或挑战码。"""
    connection = FakeConnection()
    cancels = 0
    sent = asyncio.Event()
    reserved = []

    async def write(data):
        nonlocal cancels
        if data == b"ls\n":
            await connection.received.put(PSH_LS)
        elif data == b"debug\n":
            for piece in device_challenge():
                await connection.received.put(piece)
        elif data == (PASSWORD + "\n").encode():
            await connection.received.put(b"incorrect password\r\n")
        elif data == b"\x03":
            cancels += 1
            await connection.received.put(b"Password:" if cancels == 1 else b"^C\r\n# ")
        elif data == b"ordinary\n":
            sent.set()

    connection.on_write = write
    command = {"command": "ordinary", "timeoutSeconds": .03}
    initial = [{"command": "debug", "timeoutSeconds": .03}]
    if kind == "initial":
        initial.append(command)
    scheduled = [command | {"id": "ordinary", "intervalSeconds": .01, "totalExecutions": 1}] if kind == "scheduled" else []
    collector = Collector({"id": "task", "runId": "run", "initialCommands": initial,
                           "scheduledCommands": scheduled}, tmp_path, connection_factory=lambda _: connection,
                          resolve_debug_password=lambda *_: PASSWORD,
                          reserve_execution=lambda *_: reserved.append(True) or True)
    try:
        await collector.start()
        if kind == "manual":
            await collector.enqueue_manual("ordinary", timeout_seconds=.03)
        await asyncio.wait_for(sent.wait(), .5)
        assert connection.sent.count(b"debug\n") == 1
        assert connection.sent.count((PASSWORD + "\n").encode()) == 1
        assert connection.sent[-3:] == [b"\x03", b"ls\n", b"ordinary\n"]
        assert len(reserved) == (1 if kind == "scheduled" else 0)
    finally:
        await collector.stop()


@pytest.mark.asyncio
async def test_scheduled_recovery_wait_keeps_unsent_budget(tmp_path):
    """恢复未确认仅推迟发送，下次完整间隔继续等待，不虚耗唯一执行预算。"""
    from camera_logs.collection.collector import CommandChannelBlocked

    calls, reserved, updates = [], [], []
    collector = Collector({"id": "task", "runId": "run"}, tmp_path, connection_factory=lambda _: None,
                          reserve_execution=lambda *_: reserved.append(True) or True,
                          update_execution=lambda *args: updates.append(args))

    async def enqueue(*args, **kwargs):
        calls.append(asyncio.get_running_loop().time())
        if len(calls) == 1:
            raise CommandChannelBlocked("暂未确认 shell")
        await kwargs["before_send"]()

    collector._enqueue = enqueue
    await collector._scheduled_loop(0, {"id": "ordinary", "command": "ordinary",
                                       "intervalSeconds": .01, "totalExecutions": 1})
    assert len(calls) == 2
    assert calls[1] - calls[0] >= .01
    assert len(reserved) == 1
    assert [item[1] for item in updates] == ["SENT"]
