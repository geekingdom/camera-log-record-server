"""连接适配器测试：验证协议保活和异常路径上的底层连接释放。"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import asyncssh
import pytest
from camera_logs.collection import connections
from camera_logs.collection.connections import (
    _connect_ssh,
    _connect_telnet,
    _SshConnection,
    _TelnetConnection,
)


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


def test_ssh_close_aborts_and_preserves_wait_closed_error():
    """SSH 收尾确认失败时强制中止传输，并将原始失败交给上层处理。"""
    class Process:
        def close(self):
            pass

    class Client:
        def __init__(self):
            self.closed = False
            self.aborted = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            raise ConnectionResetError("peer reset")

        def abort(self):
            self.aborted = True

    client = Client()
    with pytest.raises(ConnectionResetError, match="peer reset"):
        asyncio.run(_SshConnection(client, Process()).close())
    assert client.closed and client.aborted


def test_telnet_close_timeout_aborts_unresponsive_transport(monkeypatch):
    """Telnet 关闭确认超时后强制中止，避免暂停和停止无限等待。"""
    class UnresponsiveWriter:
        def __init__(self):
            self.closed = False
            self.aborted = False

        def get_extra_info(self, _key):
            return None

        def close(self):
            self.closed = True

        def abort(self):
            self.aborted = True

        async def wait_closed(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(connections, "CLOSE_TIMEOUT_SECONDS", .01)

    async def scenario():
        writer = UnresponsiveWriter()
        connection = _TelnetConnection(None, writer, interval=60)
        with pytest.raises(TimeoutError):
            await connection.close()
        return writer, connection

    writer, connection = asyncio.run(scenario())
    assert writer.closed and writer.aborted and connection._heartbeat.done()


def test_telnet_close_cancellation_aborts_unresponsive_transport():
    """取消关闭协程仍会中止 Telnet 传输并停止保活任务。"""
    class UnresponsiveWriter:
        def __init__(self):
            self.waiting = asyncio.Event()
            self.closed = False
            self.aborted = False

        def get_extra_info(self, _key):
            return None

        def close(self):
            self.closed = True

        def abort(self):
            self.aborted = True

        async def wait_closed(self):
            self.waiting.set()
            await asyncio.Event().wait()

    async def scenario():
        writer = UnresponsiveWriter()
        connection = _TelnetConnection(None, writer, interval=60)
        closing = asyncio.create_task(connection.close())
        await writer.waiting.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        return writer, connection

    writer, connection = asyncio.run(scenario())
    assert writer.closed and writer.aborted and connection._heartbeat.done()


def test_telnet_login_failure_preserves_original_error_after_forced_cleanup(monkeypatch):
    """登录失败后即使关闭确认超时，也不能把原始登录错误替换为清理错误。"""
    class Writer:
        def __init__(self):
            self.closed = False
            self.aborted = False

        def close(self):
            self.closed = True

        def abort(self):
            self.aborted = True

        async def wait_closed(self):
            await asyncio.Event().wait()

    writer = Writer()

    async def open_connection(**_kwargs):
        return object(), writer

    async def fail_login(*_args):
        raise RuntimeError("login rejected")

    monkeypatch.setattr(connections, "CLOSE_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr(connections, "_telnet_login", fail_login)
    monkeypatch.setitem(sys.modules, "telnetlib3", SimpleNamespace(open_connection=open_connection))
    with pytest.raises(RuntimeError, match="login rejected"):
        asyncio.run(_connect_telnet({"username": "u", "password": "p"}, "host", 23))
    assert writer.closed and writer.aborted


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


def test_ssh_shell_setup_failure_aborts_when_client_close_fails(monkeypatch, tmp_path):
    """SSH shell 创建失败时，关闭异常也不能阻止强制中止或替换原错误。"""
    class Client:
        def __init__(self):
            self.aborted = False

        async def create_process(self, **_kwargs):
            raise RuntimeError("shell unavailable")

        def close(self):
            raise ConnectionResetError("close failed")

        def abort(self):
            self.aborted = True

    client = Client()

    async def connect(*_args, **_kwargs):
        return client

    monkeypatch.setitem(sys.modules, "asyncssh", SimpleNamespace(connect=connect))
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAA")
    with pytest.raises(RuntimeError, match="shell unavailable"):
        asyncio.run(_connect_ssh({"username": "u", "password": "p", "knownHosts": known_hosts}, "host", 22))
    assert client.aborted


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


def test_ssh_connection_pool_limits_each_endpoint_to_five_sessions(monkeypatch):
    """同一设备超过五路时等待名额，关闭任一路后才允许第六路建连。"""
    class Process:
        def close(self):
            pass

    class Client:
        def __init__(self):
            self.closed = False
            self.process = Process()

        async def create_process(self, **_kwargs):
            return self.process

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    clients = []

    async def fake_connect(*_args, **_kwargs):
        client = Client()
        clients.append(client)
        return client

    monkeypatch.setitem(sys.modules, "asyncssh", SimpleNamespace(connect=fake_connect))

    async def scenario():
        tasks = [asyncio.create_task(_connect_ssh({"username": "u", "password": "p"}, "198.51.100.7", 22)) for _ in range(6)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(clients) == 5
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(tasks[5]), timeout=.01)
        first = await tasks[0]
        await first.close()
        sixth = await asyncio.wait_for(tasks[5], timeout=.5)
        for task in tasks[1:5]:
            await (await task).close()
        await sixth.close()

    asyncio.run(scenario())


def test_ssh_connection_pool_releases_slot_after_connect_failure(monkeypatch):
    """建连失败必须在底层清理完成后归还名额，后续连接不能永久饥饿。"""
    attempts = 0

    async def fake_connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionResetError("synthetic connect failure")
        return Client()

    class Process:
        def close(self):
            pass

    class Client:
        async def create_process(self, **_kwargs):
            return Process()

        def close(self):
            pass

        async def wait_closed(self):
            return None

    monkeypatch.setitem(sys.modules, "asyncssh", SimpleNamespace(connect=fake_connect))

    async def scenario():
        with pytest.raises(ConnectionResetError):
            await _connect_ssh({"username": "u", "password": "p"}, "198.51.100.8", 22)
        connection = await asyncio.wait_for(
            _connect_ssh({"username": "u", "password": "p"}, "198.51.100.8", 22), timeout=.2,
        )
        await connection.close()

    asyncio.run(scenario())


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
