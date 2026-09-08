"""手动调试恢复回归：阻断后只允许新的 debug 请求主动恢复命令通道。"""

import asyncio
import base64
import re

import pytest
from camera_logs.collection.collector import Collector, CommandChannelBlocked

CHALLENGE = base64.b64encode(b"x" * 261).decode()
PASSWORD = "synthetic-debug-password"
PREFIX = re.compile(rb"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")
PSH_LS = b"'ls' Not Supported, Try 'help'\r\n# "


class FakeConnection:
    """单连接设备替身：写入回调向唯一 reader 注入确定的设备输出。"""

    def __init__(self):
        self.sent = []
        self.received = asyncio.Queue()
        self.closed = False
        self.debug_attempts = 0
        self.cancel_attempts = 0

    async def read(self, _size=65536):
        return (await self.received.get()) or b""

    async def write(self, data):
        self.sent.append(data)
        if data == b"ls\n":
            await self.received.put(PSH_LS)
        elif data == b"debug\n":
            self.debug_attempts += 1
            await self.received.put(b"device output\n" + CHALLENGE.encode() + b"\nPassword:")
        elif data == (PASSWORD + "\n").encode():
            if self.debug_attempts == 1:
                await self.received.put(b"incorrect password\r\n")
            else:
                await self.received.put(b"BusyBox built-in shell (ash)\n# ")
        elif data == b"\x03":
            self.cancel_attempts += 1
            response = b"Password: still waiting\r\n" if self.cancel_attempts == 1 else b"# "
            await self.received.put(response)

    async def close(self):
        self.closed = True
        await self.received.put(None)


def raw_logs(chunks):
    """删除服务器时间前缀后确认设备正文持续由 reader 写入。"""
    return PREFIX.sub(b"", b"".join(chunks))


def test_blocked_manual_debug_recovers_channel_without_replaying_password_or_losing_logs(tmp_path):
    """失败握手不自动重试，后续用户 debug 可先恢复通道再开始一次新握手。"""

    async def scenario():
        logs = []
        password_calls = 0
        connection = FakeConnection()

        async def resolve(_task, _challenge):
            nonlocal password_calls
            password_calls += 1
            return PASSWORD

        collector = Collector(
            {
                "id": "task",
                "runId": "run",
                "storageIdentity": "testingdevice",
                "initialCommands": [{"command": "debug", "timeoutSeconds": 0.03}],
            },
            tmp_path,
            connection_factory=lambda _: connection,
            resolve_debug_password=resolve,
            on_log=lambda chunk: logs.append(chunk.data),
        )
        await collector.start()
        with pytest.raises(CommandChannelBlocked):
            await collector.enqueue_manual("show status")
        await connection.received.put(b"continuous raw log\r\n")
        await asyncio.sleep(0.12)
        command_id = await collector.enqueue_manual("debug", timeout_seconds=0.2)
        await collector.wait_for_command(command_id)
        await collector.stop()
        return connection.sent, password_calls, logs, collector.command_status(command_id)

    sent, password_calls, logs, status = asyncio.run(scenario())

    assert sent == [
        b"ls\n", b"debug\n", (PASSWORD + "\n").encode(), b"\x03",
        b"\x03", b"ls\n", b"debug\n", (PASSWORD + "\n").encode(),
    ]
    assert password_calls == 2
    assert status == "SENT"
    assert b"continuous raw log\r\n" in raw_logs(logs)
