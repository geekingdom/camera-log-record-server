"""Debug 口令切换采集测试：验证唯一读写链路、PSH 分包和失败隔离。"""

from __future__ import annotations

import asyncio
import base64
import re

import pytest
from camera_logs.collection.collector import Collector, PshSwitchError

# 261 个合成字节编码后恰为 348 字符，接近设备实际 PSH 密文长度但不含真实数据。
CHALLENGE = base64.b64encode(b"x" * 261).decode()
PASSWORD = "synthetic-debug-password"
PREFIX = re.compile(rb"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")
PSH_LS = b"'ls' Not Supported, Try 'help'\r\n# "


class FakeConnection:
    """按写入内容注入设备输出的单连接替身，禁止测试另开读取通道。"""

    def __init__(self, on_write=None) -> None:
        self.sent: list[bytes] = []
        self.received: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False
        self.on_write = on_write

    async def read(self, _size: int = 65536) -> bytes:
        return (await self.received.get()) or b""

    async def write(self, data: bytes) -> None:
        self.sent.append(data)
        if self.on_write:
            await self.on_write(data)

    async def close(self) -> None:
        self.closed = True
        await self.received.put(None)


def device_challenge(*, encapsulated: bool = False, fragments: bool = True) -> list[bytes]:
    """生成独立或 enc_string 格式的 PSH 输出，并可在任意位置拆包。"""
    value = (b"enc_string:\n" if encapsulated else b"device output\n") + CHALLENGE.encode() + b"\nPassword:"
    return [value[:17], value[17:211], value[211:]] if fragments else [value]


def raw_logs(chunks: list[bytes]) -> bytes:
    """移除时间前缀后比对设备原始输出，确保观察器没有吞掉 PSH 字节。"""
    return PREFIX.sub(b"", b"".join(chunks))


def test_initial_debug_switches_in_order_and_preserves_all_received_bytes(tmp_path):
    async def scenario():
        logs, events, challenges = [], [], []
        connection = FakeConnection()

        async def write(data):
            if data == b"ls\n":
                await connection.received.put(PSH_LS)
            elif data == b"debug\n":
                for piece in device_challenge(encapsulated=True):
                    await connection.received.put(piece)
            elif data == (PASSWORD + "\n").encode():
                await connection.received.put(b"BusyBox built-in shell (ash)\n# ")

        connection.on_write = write

        async def resolve(task, challenge):
            assert task["id"] == "debug-task"
            challenges.append(challenge)
            return PASSWORD

        collector = Collector(
            {"id": "debug-task", "runId": "run", "initialCommands": [{"command": "debug"}, {"command": "next"}]},
            tmp_path,
            connection_factory=lambda _: connection,
            resolve_debug_password=resolve,
            on_debug=lambda event, details: events.append((event, details)),
            on_log=lambda chunk: logs.append(chunk.data),
        )
        await collector.start()
        await connection.received.put(None)
        await collector.wait_closed()
        return connection.sent, logs, events, challenges

    sent, logs, events, challenges = asyncio.run(scenario())
    assert sent == [b"ls\n", b"debug\n", (PASSWORD + "\n").encode(), b"next\n"]
    assert challenges == [CHALLENGE]
    assert events
    assert b"".join(device_challenge(encapsulated=True)) in raw_logs(logs)
    assert b"BusyBox built-in shell (ash)\n# " in raw_logs(logs)


def test_pending_debug_provider_keeps_manual_and_scheduled_commands_behind_handshake(tmp_path):
    async def scenario():
        provider_started, release_provider = asyncio.Event(), asyncio.Event()
        connection = FakeConnection()

        async def write(data):
            if data == b"ls\n":
                await connection.received.put(PSH_LS)
            elif data == b"debug\n":
                for piece in device_challenge():
                    await connection.received.put(piece)
            elif data == (PASSWORD + "\n").encode():
                await connection.received.put(b"BusyBox built-in shell (ash)\n# ")

        connection.on_write = write

        async def resolve(_task, _challenge):
            provider_started.set()
            await release_provider.wait()
            return PASSWORD

        collector = Collector(
            {"id": "task", "runId": "run", "initialCommands": [],
             "scheduledCommands": [{"id": "later", "command": "periodic", "totalExecutions": 1, "intervalSeconds": .001}]},
            tmp_path, connection_factory=lambda _: connection, resolve_debug_password=resolve,
        )
        await collector.start()
        starting = asyncio.create_task(collector.enqueue_manual("debug"))
        await asyncio.wait_for(provider_started.wait(), 1)
        manual = asyncio.create_task(collector.enqueue_manual("manual"))
        await asyncio.sleep(.02)
        assert connection.sent == [b"ls\n", b"debug\n"]
        release_provider.set()
        await starting
        await manual
        await asyncio.sleep(.03)
        await collector.stop()
        return connection.sent

    sent = asyncio.run(scenario())
    assert sent[:3] == [b"ls\n", b"debug\n", (PASSWORD + "\n").encode()]
    assert sent.index(b"manual\n") > sent.index((PASSWORD + "\n").encode())
    assert sent.index(b"periodic\n") > sent.index((PASSWORD + "\n").encode())


def test_existing_busybox_ash_skips_debug_provider(tmp_path):
    async def scenario():
        calls = 0
        connection = FakeConnection()

        async def resolve(_task, _challenge):
            nonlocal calls
            calls += 1
            return PASSWORD

        collector = Collector(
            {"id": "task", "runId": "run", "initialCommands": []},
            tmp_path, connection_factory=lambda _: connection, resolve_debug_password=resolve,
        )
        await collector.start()
        # 先由唯一 reader 消费登录横幅，再用手动 debug 走相同发送链路。
        await connection.received.put(b"BusyBox built-in shell (ash)\n# ")
        await asyncio.sleep(0)
        await collector.enqueue_manual(" debug ")
        await collector.enqueue_manual("next")
        await collector.stop()
        return calls, connection.sent

    calls, sent = asyncio.run(scenario())
    assert calls == 0
    assert sent == [b"next\n"]


def test_initial_debug_waits_for_existing_ash_banner_and_skips_write(tmp_path):
    async def scenario():
        calls = 0
        connection = FakeConnection()
        await connection.received.put(b"BusyBox built-in shell (ash)\n# ")

        async def resolve(_task, _challenge):
            nonlocal calls
            calls += 1
            return PASSWORD

        collector = Collector(
            {"id": "task", "runId": "run", "initialCommands": [{"command": "debug"}, {"command": "next"}]},
            tmp_path, connection_factory=lambda _: connection, resolve_debug_password=resolve,
        )
        await collector.start()
        await collector.stop()
        return calls, connection.sent

    calls, sent = asyncio.run(scenario())
    assert calls == 0
    assert sent == [b"next\n"]


def test_scheduled_debug_consumes_one_budget_for_whole_switch(tmp_path):
    async def scenario():
        reserved, updates = [], []
        completed = asyncio.Event()
        connection = FakeConnection()

        async def write(data):
            if data == b"ls\n":
                await connection.received.put(PSH_LS)
            elif data == b"debug\n":
                for piece in device_challenge():
                    await connection.received.put(piece)
            elif data == (PASSWORD + "\n").encode():
                await connection.received.put(b"BusyBox built-in shell (ash)\n# ")

        connection.on_write = write
        async def resolve(_task, _challenge):
            return PASSWORD

        async def update(command_id, state, detail):
            updates.append((command_id, state, detail))
            if state == "SENT":
                completed.set()

        collector = Collector(
            {"id": "task", "runId": "run", "initialCommands": [],
             "scheduledCommands": [{"id": "debug-periodic", "command": "debug", "totalExecutions": 1, "intervalSeconds": .001}]},
            tmp_path,
            connection_factory=lambda _: connection,
            resolve_debug_password=resolve,
            reserve_execution=lambda command_id, detail: reserved.append((command_id, detail)) or True,
            update_execution=update,
        )
        await collector.start()
        await asyncio.wait_for(completed.wait(), 2)
        await collector.stop()
        return connection.sent, reserved, updates

    sent, reserved, updates = asyncio.run(scenario())
    assert sent == [b"ls\n", b"debug\n", (PASSWORD + "\n").encode()]
    assert len(reserved) == 1 and reserved[0][0] == "debug-periodic"
    assert [state for _identifier, state, _detail in updates] == ["SENT"]


def test_login_help_banner_before_challenge_does_not_send_fallback_command(tmp_path):
    async def scenario():
        connection = FakeConnection()

        async def write(data):
            if data == b"ls\n":
                await connection.received.put(PSH_LS)
            elif data == b"debug\n":
                await connection.received.put(b"Enter help..\n# \n")
                for piece in device_challenge():
                    await connection.received.put(piece)
            elif data == (PASSWORD + "\n").encode():
                await connection.received.put(b"BusyBox built-in shell (ash)\n# ")

        connection.on_write = write

        async def resolve(_task, _challenge):
            return PASSWORD

        collector = Collector(
            {"id": "task", "runId": "run", "initialCommands": [{"command": "debug"}]},
            tmp_path, connection_factory=lambda _: connection, resolve_debug_password=resolve,
        )
        await collector.start()
        await collector.stop()
        return connection.sent

    sent = asyncio.run(scenario())
    assert sent == [b"ls\n", b"debug\n", (PASSWORD + "\n").encode()]


def test_unknown_mode_normal_ls_echo_confirms_ash_without_debug_or_password(tmp_path):
    async def scenario():
        calls = 0
        connection = FakeConnection()

        async def write(data):
            if data == b"ls\n":
                await connection.received.put(b"ls\r\nbin etc proc\r\n# ")

        connection.on_write = write

        async def resolve(_task, _challenge):
            nonlocal calls
            calls += 1
            return PASSWORD

        collector = Collector(
            {"id": "task", "runId": "run", "initialCommands": [{"command": "debug"}, {"command": "next"}]},
            tmp_path, connection_factory=lambda _: connection, resolve_debug_password=resolve,
        )
        await collector.start()
        await collector.stop()
        return calls, connection.sent

    calls, sent = asyncio.run(scenario())
    assert calls == 0
    assert sent == [b"ls\n", b"next\n"]


def test_password_prompt_without_ash_banner_rechecks_with_ls_before_next_command(tmp_path):
    async def scenario():
        probes = 0
        connection = FakeConnection()

        async def write(data):
            nonlocal probes
            if data == b"ls\n":
                probes += 1
                await connection.received.put(PSH_LS if probes == 1 else b"ls\r\nbin etc proc\r\n# ")
            elif data == b"debug\n":
                for piece in device_challenge():
                    await connection.received.put(piece)
            elif data == (PASSWORD + "\n").encode():
                await connection.received.put(b"# ")

        connection.on_write = write

        async def resolve(_task, _challenge):
            return PASSWORD

        collector = Collector(
            {"id": "task", "runId": "run", "initialCommands": [{"command": "debug"}, {"command": "next"}]},
            tmp_path, connection_factory=lambda _: connection, resolve_debug_password=resolve,
        )
        await collector.start()
        await collector.stop()
        return connection.sent

    assert asyncio.run(scenario()) == [b"ls\n", b"debug\n", (PASSWORD + "\n").encode(), b"ls\n", b"next\n"]


@pytest.mark.parametrize("reply", [b"incorrect password\n", b"unrecognized prompt>\n", None])
def test_debug_failures_do_not_send_following_command(tmp_path, reply):
    async def scenario():
        connection = FakeConnection()

        async def write(data):
            if data == b"ls\n":
                await connection.received.put(PSH_LS)
            elif data == b"debug\n":
                for piece in device_challenge():
                    await connection.received.put(piece)
            elif data == (PASSWORD + "\n").encode():
                await connection.received.put(reply)

        connection.on_write = write
        collector = Collector(
            {"id": "task", "runId": "run", "initialCommands": [{"command": "debug", "timeoutSeconds": .05}, {"command": "next"}]},
            tmp_path, connection_factory=lambda _: connection,
            resolve_debug_password=lambda _task, _challenge: PASSWORD,
        )
        with pytest.raises(PshSwitchError):
            await collector.start()
        return connection.sent, connection.closed

    sent, closed = asyncio.run(scenario())
    assert b"next\n" not in sent
    assert closed


def test_only_trimmed_exact_debug_command_is_intercepted(tmp_path):
    async def scenario():
        calls = 0
        connection = FakeConnection()

        async def resolve(_task, _challenge):
            nonlocal calls
            calls += 1
            return PASSWORD

        collector = Collector(
            {"id": "task", "runId": "run", "initialCommands": [{"command": "echo debug"}]},
            tmp_path, connection_factory=lambda _: connection, resolve_debug_password=resolve,
        )
        await collector.start()
        await collector.stop()
        return calls, connection.sent

    calls, sent = asyncio.run(scenario())
    assert calls == 0
    assert sent == [b"echo debug\n"]
