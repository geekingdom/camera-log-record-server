"""验证从机登录单接收器、主机输出隔离及共享端口引导边界，不访问真实设备。"""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncssh
import pytest
from camera_logs.collection.slave_shell import ShellBootstrap
from camera_logs.collection.slave_ssh import connect_slave, dropbear_command


class Shell:
    """按写入阶段返回分包横幅，保留命令用于验证口令仅在正确阶段发送。"""
    def __init__(self, *, accepted=True, address=b"192.168.253.164"):
        self.queue = asyncio.Queue()
        self.queue.put_nowait(b"BusyBox main built-in shell (ash)\r\n# ")
        self.writes = []
        self.closed = False
        self.accepted = accepted
        self.address = address

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
        elif data.startswith(b"ifconfig;"):
            marker = re.search(rb"(__SLAVE_BOOT_[0-9a-f]+):%s", data).group(1)
            self.queue.put_nowait(b"eth0 Link encap:Ethernet\n inet addr:" + self.address + b"\n")
            self.queue.put_nowait(b"\n" + marker + b":0\n")
        elif data == b"debug\n":
            self.queue.put_nowait(b"-ash: debug: not found\n# ")

    async def close(self):
        self.closed = True


async def test_slave_fragmented_login_excludes_host_and_password_prompt():
    connection = Shell()
    shell = ShellBootstrap(connection, {"protocol": "SSH"})
    try:
        await shell.enter_slave("SLAVE_1", "secret")
        connection.queue.put_nowait(b"slave-log\r\n")
        data = await asyncio.wait_for(shell.read(), 1)
        assert b"slave-log" in data
        assert b"main" not in data and b"password" not in data
        assert connection.writes[0].startswith(b"dbclient admin@192.168.253.164 -y; printf ")
        assert connection.writes[0].endswith(b"; exit\n")
        assert connection.writes[1] == b"secret\n"
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


async def test_slave_wrong_interface_ip_never_enters_verified_logging_mode():
    connection = Shell(address=b"192.168.253.199")
    shell = ShellBootstrap(connection, {"protocol": "SSH"})
    try:
        with pytest.raises(Exception, match="接口地址"):
            await shell.enter_slave("SLAVE_1", "secret")
        assert not shell.ready and not shell.bootstrap_verified
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


@pytest.mark.parametrize('output,status', [
    (b'inet addr:192.168.253.165\n', 0),
    (b'inet addr:192.168.253.164.9\n', 0),
    (b'inet addr:192.168.253.164.example\n', 0),
    (b'ifconfig 192.168.253.164\n# ', 0),
    (b'inet 192.168.253.164\n', 1),
])
async def test_slave_identity_never_accepts_wrong_echo_or_failed_command(output, status):
    """只接受成功ifconfig的完整inet地址字段，回显、前缀与失败退出均不得交接。"""
    connection = Shell()
    original = connection.write

    async def write(data):
        if data.startswith(b'ifconfig;'):
            marker = re.search(rb'(__SLAVE_BOOT_[0-9a-f]+):%s', data).group(1)
            connection.queue.put_nowait(output + b'\n' + marker + f':{status}\n'.encode())
        else:
            await original(data)

    connection.write = write
    shell = ShellBootstrap(connection, {'protocol': 'SSH'})
    try:
        with pytest.raises(ConnectionError, match='接口地址'):
            await shell.enter_slave('SLAVE_1', 'secret')
        assert not shell.ready and not shell.bootstrap_verified
    finally:
        await shell.close()


async def test_slave_valid_ip_without_completion_marker_is_not_verified():
    """IP文字已经到达但响应中途断开，不能把半个ifconfig当成成功。"""
    connection = Shell()
    original = connection.write

    async def write(data):
        if data.startswith(b'ifconfig;'):
            connection.queue.put_nowait(b'inet addr:192.168.253.164\n')
            connection.queue.put_nowait(b'')
        else:
            await original(data)

    connection.write = write
    shell = ShellBootstrap(connection, {'protocol': 'SSH'})
    try:
        with pytest.raises(ConnectionError):
            await shell.enter_slave('SLAVE_1', 'secret')
        assert not shell.ready
    finally:
        await shell.close()


@pytest.mark.parametrize('split', [1, 5, 20, 43])
async def test_slave_exit_marker_blocks_parent_psh_bytes(split):
    """dbclient退出仅回主机PSH时仍截断主机字节，跨包边界不泄漏且保留之前从机日志。"""
    connection = Shell()
    shell = ShellBootstrap(connection, {'protocol': 'SSH', 'sshTarget': 'SLAVE_1'})
    try:
        await shell.enter_slave('SLAVE_1', 'secret')
        stream = b'slave-final\n' + shell.exit_marker + b'HOST LOG\nProtect Shell (psh)\n# '
        boundary = len(b'slave-final\n') + min(split, len(shell.exit_marker))
        connection.queue.put_nowait(stream[:boundary])
        connection.queue.put_nowait(stream[boundary:])
        received = b''
        async with asyncio.timeout(1):
            while data := await shell.read():
                received += data
        assert received == b'slave-final\n'
        assert shell.identity_lost
        with pytest.raises(ConnectionError):
            await shell.write(b'ifconfig\n')
    finally:
        await shell.close()


async def test_slave_rejects_host_external_address_even_with_target_alias():
    """主机即使存在目标地址别名，也不能以此冒充已经进入从机。"""
    connection = Shell(address=b'192.168.253.164\n inet addr:10.41.203.35')
    shell = ShellBootstrap(connection, {'protocol': 'SSH', 'ip': '10.41.203.35', 'sshTarget': 'SLAVE_1'})
    try:
        with pytest.raises(ConnectionError, match='接口地址'):
            await shell.enter_slave('SLAVE_1', 'secret')
        assert not shell.bootstrap_verified
    finally:
        await shell.close()


async def test_collector_rejects_verified_connection_for_another_slave(tmp_path):
    """即使缓存为已验证ASH，从机1连接也不能交给从机2任务使用。"""
    from camera_logs.collection.collector import Collector
    connection = Shell()
    shell = ShellBootstrap(connection, {'protocol': 'SSH', 'sshTarget': 'SLAVE_1'})
    await shell.enter_slave('SLAVE_1', 'secret')
    collector = Collector({'id': 'wrong-target', 'runId': 'run', 'storageIdentity': 'test',
                           'protocol': 'SSH', 'sshTarget': 'SLAVE_2'}, tmp_path,
                          connection_factory=AsyncMock(return_value=shell))
    try:
        with pytest.raises(ConnectionError, match='目标IP'):
            await collector.start()
        assert connection.closed
    finally:
        await collector.stop()
