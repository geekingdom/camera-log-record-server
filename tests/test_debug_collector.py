"""Debug 口令切换采集测试：验证唯一读写链路、PSH 分包和失败隔离。"""

from __future__ import annotations

import asyncio
import base64
import re

import pytest
from camera_logs.collection.collector import Collector as RuntimeCollector

# 261 个合成字节编码后恰为 348 字符，接近设备实际 PSH 密文长度但不含真实数据。
CHALLENGE = base64.b64encode(b"x" * 261).decode()
PASSWORD = "synthetic-debug-password"
PREFIX = re.compile(rb"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")
PSH_LS = b"'ls' Not Supported, Try 'help'\r\n# "


def Collector(task, *args, **kwargs):
    """为本模块所有合成采集任务显式附加统一的设备存储身份。"""
    return RuntimeCollector({"storageIdentity": "testingdevice", **task}, *args, **kwargs)


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


def test_debug_failure_recovers_command_channel_and_preserves_logs_without_second_password(tmp_path):
    """debug 失败后 Ctrl-C 和无密码 ls 确认 PSH 提示符，后续命令、定时项和日志继续。"""
    async def scenario():
        logs, reserved, updates, events = [], [], [], []
        scheduled = asyncio.Event()
        password_calls = 0
        probes = 0
        connection = FakeConnection()

        async def write(data):
            nonlocal password_calls, probes
            if data == b"ls\n":
                probes += 1
                await connection.received.put(PSH_LS)
            elif data == b"debug\n":
                for piece in device_challenge():
                    await connection.received.put(piece)
            elif data == (PASSWORD + "\n").encode():
                password_calls += 1
                await connection.received.put(b"incorrect password\r\n")
            elif data == b"\x03":
                await connection.received.put(b"^C\r\n# ")
            elif data == b"next\n":
                await connection.received.put(b"next output\r\n")
            elif data == b"manual\n":
                await connection.received.put(b"manual output\r\n")
            elif data == b"periodic\n":
                await connection.received.put(b"periodic output\r\n")

        connection.on_write = write
        async def update(identifier, state, detail):
            updates.append((identifier, state, detail))
            if identifier == "periodic" and state == "SENT":
                scheduled.set()
        collector = Collector(
            {
                "id": "task", "runId": "run",
                "initialCommands": [{"command": "debug", "timeoutSeconds": .05}, {"command": "next"}],
                "scheduledCommands": [{"id": "periodic", "command": "periodic", "totalExecutions": 1, "intervalSeconds": .001}],
            },
            tmp_path, connection_factory=lambda _: connection,
            resolve_debug_password=lambda _task, _challenge: PASSWORD,
            reserve_execution=lambda identifier, detail: reserved.append((identifier, detail)) or True,
            update_execution=update,
            on_log=lambda chunk: logs.append(chunk.data),
            on_debug=lambda event, details: events.append((event, details)),
        )
        await collector.start()
        await collector.enqueue_manual("manual")
        await asyncio.wait_for(scheduled.wait(), 1)
        await connection.received.put(None)
        await collector.wait_closed()
        return connection.sent, logs, password_calls, probes, reserved, updates, events, connection.closed

    sent, logs, password_calls, probes, reserved, updates, events, closed = asyncio.run(scenario())
    assert sent == [b"ls\n", b"debug\n", (PASSWORD + "\n").encode(), b"\x03", b"ls\n", b"next\n", b"manual\n", b"periodic\n"]
    assert password_calls == 1 and probes == 2
    assert reserved and reserved[0][0] == "periodic"
    assert [state for _identifier, state, _detail in updates] == ["SENT"]
    assert [event for event, _details in events][-2:] == ["FAILED", "RECOVERED"]
    assert events[-1][1] == {"mode": "PSH", "commandBlocked": False, "debugError": "PSH 调试失败，本次命令未自动重试"}
    assert b"".join(device_challenge()) in raw_logs(logs)
    assert b"next output\r\nmanual output\r\nperiodic output\r\n" in raw_logs(logs)
    assert closed


def test_unconfirmed_debug_recovery_blocks_commands_without_stopping_log_reader_or_spending_budget(tmp_path):
    """取消和 ls 都未获确认时继续接收日志，普通手动及定时命令均不写入设备。"""
    async def scenario():
        logs, reserved, updates, events = [], [], [], []
        connection = FakeConnection()

        async def write(data):
            if data == b"ls\n" and b"debug\n" not in connection.sent:
                await connection.received.put(PSH_LS)
            elif data == b"debug\n":
                for piece in device_challenge():
                    await connection.received.put(piece)
            elif data == (PASSWORD + "\n").encode():
                await connection.received.put(b"incorrect password\r\n")
            elif data == b"\x03":
                await connection.received.put(b"Password: still waiting\r\n")

        connection.on_write = write
        collector = Collector(
            {
                "id": "task", "runId": "run",
                "initialCommands": [{"command": "debug", "timeoutSeconds": .03}, {"command": "next"}],
                "scheduledCommands": [{"id": "blocked", "command": "periodic", "totalExecutions": 1, "intervalSeconds": .001}],
            },
            tmp_path, connection_factory=lambda _: connection,
            resolve_debug_password=lambda _task, _challenge: PASSWORD,
            reserve_execution=lambda identifier, detail: reserved.append((identifier, detail)) or True,
            update_execution=lambda identifier, state, detail: updates.append((identifier, state, detail)),
            on_log=lambda chunk: logs.append(chunk.data),
            on_debug=lambda event, details: events.append((event, details)),
        )
        await collector.start()
        with pytest.raises(RuntimeError, match="暂不可发送命令"):
            await collector.enqueue_manual("manual")
        await connection.received.put(b"continuous raw log\r\n")
        await asyncio.sleep(.05)
        await collector.stop()
        return connection.sent, logs, reserved, updates, events

    sent, logs, reserved, updates, events = asyncio.run(scenario())
    assert sent == [b"ls\n", b"debug\n", (PASSWORD + "\n").encode(), b"\x03"]
    assert reserved == [] and updates == []
    assert [event for event, _details in events][-2:] == ["FAILED", "BLOCKED"]
    assert events[-1][1]["commandBlocked"] is True
    assert b"continuous raw log\r\n" in raw_logs(logs)


def test_scheduled_debug_failure_records_current_attempt_then_allows_next_attempt(tmp_path):
    """首次定时 debug 失败后，下一次定时 debug 先安全恢复再独立握手。"""
    async def scenario():
        logs, reserved, updates = [], [], []
        failed = asyncio.Event()
        password_calls = 0
        connection = FakeConnection()

        async def write(data):
            nonlocal password_calls
            if data == b"ls\n":
                await connection.received.put(PSH_LS)
            elif data == b"debug\n":
                for piece in device_challenge():
                    await connection.received.put(piece)
            elif data == (PASSWORD + "\n").encode():
                password_calls += 1
                await connection.received.put(
                    b"incorrect password\r\n" if password_calls == 1 else b"BusyBox built-in shell (ash)\n# ",
                )
            elif data == b"\x03":
                await connection.received.put(b"^C\r\n# ")

        connection.on_write = write
        async def update(identifier, state, detail):
            updates.append((identifier, state, detail))
            if state == "SENT":
                failed.set()
        collector = Collector(
            {"id": "task", "runId": "run", "initialCommands": [],
             "scheduledCommands": [{"id": "debug-periodic", "command": "debug", "totalExecutions": 2, "intervalSeconds": .001}]},
            tmp_path, connection_factory=lambda _: connection,
            resolve_debug_password=lambda _task, _challenge: PASSWORD,
            reserve_execution=lambda identifier, detail: reserved.append((identifier, detail)) or True,
            update_execution=update, on_log=lambda chunk: logs.append(chunk.data),
        )
        await collector.start()
        await asyncio.wait_for(failed.wait(), 1)
        await connection.received.put(b"continued after scheduled failure\r\n")
        await asyncio.sleep(.12)
        await collector.stop()
        return connection.sent, reserved, updates, password_calls, logs

    sent, reserved, updates, password_calls, logs = asyncio.run(scenario())
    assert sent == [b"ls\n", b"debug\n", (PASSWORD + "\n").encode(), b"\x03", b"ls\n", b"debug\n", (PASSWORD + "\n").encode()]
    assert len(reserved) == 2 and {entry[0] for entry in reserved} == {"debug-periodic"}
    assert [(identifier, state) for identifier, state, _detail in updates] == [("debug-periodic", "FAILED"), ("debug-periodic", "SENT")]
    assert password_calls == 2
    assert b"continued after scheduled failure\r\n" in raw_logs(logs)


def test_reconnected_collector_retries_initial_debug_as_a_new_command(tmp_path):
    """断线后的新 Collector 不继承前次失败，可再次执行初始化 debug 并完成新的单次握手。"""
    async def scenario():
        first, second = FakeConnection(), FakeConnection()

        async def first_write(data):
            if data == b"ls\n":
                await first.received.put(PSH_LS)
            elif data == b"debug\n":
                for piece in device_challenge():
                    await first.received.put(piece)
            elif data == (PASSWORD + "\n").encode():
                await first.received.put(b"incorrect password\r\n")
            elif data == b"\x03":
                await first.received.put(b"^C\r\n# ")

        async def second_write(data):
            if data == b"ls\n":
                await second.received.put(PSH_LS)
            elif data == b"debug\n":
                for piece in device_challenge():
                    await second.received.put(piece)
            elif data == (PASSWORD + "\n").encode():
                await second.received.put(b"BusyBox built-in shell (ash)\n# ")

        first.on_write, second.on_write = first_write, second_write
        task = {"id": "task", "runId": "same-run", "initialCommands": [{"command": "debug", "timeoutSeconds": .05}]}
        failed = Collector(task, tmp_path, connection_factory=lambda _: first, resolve_debug_password=lambda *_args: PASSWORD)
        await failed.start()
        await failed.stop()
        reconnected = Collector(task, tmp_path, connection_factory=lambda _: second, resolve_debug_password=lambda *_args: PASSWORD)
        await reconnected.start()
        await reconnected.stop()
        return first.sent, second.sent

    first_sent, second_sent = asyncio.run(scenario())
    assert first_sent == [b"ls\n", b"debug\n", (PASSWORD + "\n").encode(), b"\x03", b"ls\n"]
    assert second_sent == [b"ls\n", b"debug\n", (PASSWORD + "\n").encode()]


def test_cancel_write_failure_blocks_commands_but_keeps_log_reader_running(tmp_path):
    """Ctrl-C 写入失败也必须进入命令阻断，不能假设设备已退出密码提示。"""
    async def scenario():
        logs = []
        connection = FakeConnection()

        async def write(data):
            if data == b"ls\n":
                await connection.received.put(PSH_LS)
            elif data == b"debug\n":
                for piece in device_challenge():
                    await connection.received.put(piece)
            elif data == (PASSWORD + "\n").encode():
                await connection.received.put(b"incorrect password\r\n")
            elif data == b"\x03":
                raise OSError("synthetic cancel write failure")

        connection.on_write = write
        collector = Collector(
            {"id": "task", "runId": "run", "initialCommands": [{"command": "debug", "timeoutSeconds": .03}]},
            tmp_path, connection_factory=lambda _: connection,
            resolve_debug_password=lambda _task, _challenge: PASSWORD,
            on_log=lambda chunk: logs.append(chunk.data),
        )
        await collector.start()
        with pytest.raises(RuntimeError, match="暂不可发送命令"):
            await collector.enqueue_manual("manual")
        await connection.received.put(b"reader survives cancel write failure\r\n")
        await asyncio.sleep(.12)
        await collector.stop()
        return connection.sent, logs

    sent, logs = asyncio.run(scenario())
    assert sent == [b"ls\n", b"debug\n", (PASSWORD + "\n").encode(), b"\x03"]
    assert b"reader survives cancel write failure\r\n" in raw_logs(logs)


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
