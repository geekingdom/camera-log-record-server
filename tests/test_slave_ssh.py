"""验证从机登录单接收器、主机输出隔离及共享端口引导边界，不访问真实设备。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncssh
import pytest
from camera_logs.collection.slave_shell import ShellBootstrap
from camera_logs.collection.slave_ssh import connect_slave, dropbear_command


class Shell:
    """按写入阶段返回分包横幅，保留命令用于验证口令仅在正确阶段发送。"""
    def __init__(self, *, accepted=True):
        self.queue = asyncio.Queue()
        self.queue.put_nowait(b"BusyBox main built-in shell (ash)\r\n# ")
        self.writes = []
        self.closed = False
        self.accepted = accepted

    async def read(self, size):
        return await self.queue.get()

    async def write(self, data):
        self.writes.append(data)
        if data.startswith(b"dbclient"):
            self.queue.put_nowait(b"admin@192.168.253.164's pass")
            self.queue.put_nowait(b"word: ")
        elif data == b"secret\n":
            if self.accepted:
                self.queue.put_nowait(b"BusyBox slave built-in shell (as")
                self.queue.put_nowait(b"h)\r\n# slave-log\r\n")
            else:
                self.queue.put_nowait(b"Permission denied\r\n")
                self.queue.put_nowait(b"")

    async def close(self):
        self.closed = True


async def test_slave_fragmented_login_excludes_host_and_password_prompt():
    connection = Shell()
    shell = ShellBootstrap(connection, {"protocol": "SSH"})
    try:
        await shell.enter_slave("SLAVE_1", "secret")
        data = await shell.read()
        assert b"slave-log" in data
        assert b"main" not in data and b"password" not in data
        assert connection.writes == [b"dbclient admin@192.168.253.164 -y; exit\n", b"secret\n"]
        connection.queue.put_nowait(b"last-log\n")
        connection.queue.put_nowait(b"")
        assert await shell.read() == b"last-log\n"
        assert await shell.read() == b""
    finally:
        await shell.close()
    assert connection.closed and shell.reader.done()


async def test_failed_slave_auth_never_enters_logging_mode():
    connection = Shell(accepted=False)
    shell = ShellBootstrap(connection, {"protocol": "SSH"})
    try:
        with pytest.raises(asyncssh.PermissionDenied):
            await shell.enter_slave("SLAVE_1", "secret")
        assert not shell.ready
        assert connection.writes.count(b"secret\n") == 1
    finally:
        await shell.close()


async def test_cancelled_handshake_closes_reader_and_transport():
    connection = Shell()
    shell = ShellBootstrap(connection, {"protocol": "SSH"})
    task = asyncio.create_task(shell.enter_slave("SLAVE_2", "secret"))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await shell.close()
    assert shell.reader.done() and connection.closed


@pytest.mark.parametrize("port", [22, 18079, 18085, "18080", True])
def test_bootstrap_command_rejects_arbitrary_port(port):
    with pytest.raises(ValueError):
        dropbear_command(port)


async def test_existing_shared_service_skips_host_bootstrap(monkeypatch):
    from camera_logs.collection import slave_ssh
    config = {"id": "slave", "resourceId": "resource", "ip": "10.0.0.35", "port": 22,
              "sshTarget": "SLAVE_1", "password": "secret", "runId": "run", "nodeId": "node", "generation": 1}
    repo = SimpleNamespace(settings=SimpleNamespace(), audit=AsyncMock(),
                           db=SimpleNamespace(tasks=SimpleNamespace(update_one=AsyncMock())))
    monkeypatch.setattr(slave_ssh, "PshPasswordProvider", lambda _: None)
    monkeypatch.setattr(slave_ssh, "shared_port", AsyncMock(return_value=18080))
    monkeypatch.setattr(slave_ssh, "active_task", AsyncMock(return_value={}))
    monkeypatch.setattr(slave_ssh, "ssh_service_available", AsyncMock(return_value=True))
    borrow = AsyncMock()
    monkeypatch.setattr(slave_ssh, "borrow_host", borrow)
    connection = Shell()
    direct = AsyncMock(return_value=connection)
    shell = await connect_slave(SimpleNamespace(repo=repo), config, direct)
    try:
        assert direct.call_args.args[0]["port"] == 18080
        assert config["port"] == 22
        borrow.assert_not_awaited()
    finally:
        await shell.close()


async def test_host_debug_failure_is_retryable_without_slave_initialization(monkeypatch):
    """父机debug一次失败不能永久终止从机任务，但本次绝不能直接执行从机初始化。"""
    from camera_logs.collection import slave_ssh
    from camera_logs.collection.psh_dialogue import PshSwitchError
    from camera_logs.collection.slave_shell import SlaveLoginError

    monkeypatch.setattr(slave_ssh, "shared_port", AsyncMock(return_value=18080))
    monkeypatch.setattr(slave_ssh, "ensure_service", AsyncMock(side_effect=PshSwitchError("failed")))
    direct = AsyncMock()
    with pytest.raises(SlaveLoginError, match="后续重连"):
        await connect_slave(SimpleNamespace(repo=object()), {}, direct)
    direct.assert_not_awaited()
