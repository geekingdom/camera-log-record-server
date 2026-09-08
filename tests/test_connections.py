"""连接适配器测试：验证协议保活和异常路径上的底层连接释放。"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest
from camera_logs.collection.connections import _connect_ssh, _TelnetConnection


class Writer:
    def __init__(self): self.nops = 0; self.closed = False
    def iac(self, value): self.nops += 1; assert value == b"\xf1"
    async def drain(self): pass
    def close(self): self.closed = True
    async def wait_closed(self): pass


def test_telnet_heartbeat_uses_iac_nop_and_stops_on_close():
    async def run():
        writer = Writer()
        connection = _TelnetConnection(None, writer, interval=.01)
        await asyncio.sleep(.025)
        await connection.close()
        count = writer.nops
        await asyncio.sleep(.02)
        return count, writer.nops
    before, after = asyncio.run(run())
    assert before > 0
    assert before == after


def test_ssh_shell_setup_failure_closes_owned_client(monkeypatch, tmp_path):
    class Client:
        def __init__(self): self.closed = self.aborted = False
        async def create_process(self, **_kwargs): raise RuntimeError("shell unavailable")
        def close(self): self.closed = True
        async def wait_closed(self): return None
        def abort(self): self.aborted = True

    client = Client()
    async def connect(*_args, **_kwargs): return client
    monkeypatch.setitem(sys.modules, "asyncssh", SimpleNamespace(connect=connect))
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAA")
    with pytest.raises(RuntimeError, match="shell unavailable"):
        asyncio.run(_connect_ssh({"username": "u", "password": "p", "knownHosts": known_hosts}, "host", 22))
    assert client.closed


def test_ssh_shell_setup_cancellation_closes_owned_client(monkeypatch, tmp_path):
    class Client:
        def __init__(self): self.closed = self.aborted = False
        async def create_process(self, **_kwargs): await asyncio.Event().wait()
        def close(self): self.closed = True
        async def wait_closed(self): return None
        def abort(self): self.aborted = True

    client = Client()
    async def connect(*_args, **_kwargs): return client
    monkeypatch.setitem(sys.modules, "asyncssh", SimpleNamespace(connect=connect))
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAA")

    async def scenario():
        task = asyncio.create_task(_connect_ssh({"username": "u", "password": "p", "knownHosts": known_hosts}, "host", 22))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert client.closed
