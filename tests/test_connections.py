"""连接适配器测试：验证协议保活和异常路径上的底层连接释放。"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import asyncssh
import pytest
from camera_logs.collection.connections import _connect_ssh, _TelnetConnection


class Writer:
    def __init__(self): self.nops = 0; self.closed = False
    def send_iac(self, value): self.nops += 1; assert value == b"\xff\xf1"
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


def test_default_ssh_connection_allows_first_connection_and_changed_host_key():
    """默认模式不登记指纹，同一端口更换设备公钥后仍按账号密码成功连接。"""
    class Server(asyncssh.SSHServer):
        def begin_auth(self, _username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "u" and password == "p"

    async def process(process):
        process.exit(0)

    async def start(port, key):
        return await asyncssh.listen(
            "127.0.0.1", port, server_factory=Server, process_factory=process, server_host_keys=[key],
        )

    async def scenario():
        first_server = await start(0, asyncssh.generate_private_key("ssh-ed25519"))
        port = first_server.get_port()
        first = await _connect_ssh({"username": "u", "password": "p"}, "127.0.0.1", port)
        await first.close()
        first_server.close()
        await first_server.wait_closed()

        replacement_server = await start(port, asyncssh.generate_private_key("ssh-ed25519"))
        try:
            replacement = await _connect_ssh({"username": "u", "password": "p"}, "127.0.0.1", port)
            await replacement.close()
        finally:
            replacement_server.close()
            await replacement_server.wait_closed()

    asyncio.run(scenario())


def test_strict_ssh_connection_requires_configured_known_hosts(monkeypatch):
    """显式启用严格模式时，缺少本地指纹文件会在认证前失败。"""
    connect = pytest.fail
    monkeypatch.setitem(sys.modules, "asyncssh", SimpleNamespace(connect=connect))
    with pytest.raises(ValueError, match="严格SSH主机指纹校验"):
        asyncio.run(_connect_ssh({"username": "u", "password": "p", "verifyHostKey": True}, "host", 22))


def test_strict_ssh_connection_rejects_changed_host_key(tmp_path):
    """可选严格模式将已登记的同端点公钥交给 AsyncSSH，替换公钥必须被拒绝。"""
    class Server(asyncssh.SSHServer):
        def begin_auth(self, _username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "u" and password == "p"

    async def process(process):
        process.exit(0)

    async def start(port, key):
        return await asyncssh.listen(
            "127.0.0.1", port, server_factory=Server, process_factory=process, server_host_keys=[key],
        )

    async def scenario():
        expected_key = asyncssh.generate_private_key("ssh-ed25519")
        first_server = await start(0, expected_key)
        port = first_server.get_port()
        known_hosts = tmp_path / "known_hosts"
        known_hosts.write_bytes(f"[127.0.0.1]:{port} ".encode() + expected_key.export_public_key())
        config = {"username": "u", "password": "p", "verifyHostKey": True, "knownHosts": known_hosts}
        connection = await _connect_ssh(config, "127.0.0.1", port)
        await connection.close()
        first_server.close()
        await first_server.wait_closed()

        replacement_server = await start(port, asyncssh.generate_private_key("ssh-ed25519"))
        try:
            with pytest.raises(asyncssh.HostKeyNotVerifiable):
                await _connect_ssh(config, "127.0.0.1", port)
        finally:
            replacement_server.close()
            await replacement_server.wait_closed()

    asyncio.run(scenario())
