"""从机切换后的调试入口回归：复用主机解密链路，不依赖公司实机或解密服务。"""

import asyncio
import base64

import pytest
from camera_logs.collection.collector import Collector
from camera_logs.collection.slave_shell import ShellBootstrap

CHALLENGE = base64.b64encode(b'slave-only-challenge' * 20).decode()


class NestedShell:
    """模拟主机ASH、dbclient登录及从机PSH，记录每次发送时所在的真实shell。"""

    def __init__(self, *, advertised_mode='psh', actual_mode='psh'):
        self.queue = asyncio.Queue()
        self.queue.put_nowait(b'BusyBox host built-in shell (ash)\n# ')
        self.mode = actual_mode
        self.advertised_mode = advertised_mode
        self.in_slave = False
        self.sent = []
        self.closed = False
        self.ash_ready = asyncio.Event()
        self.readers = 0
        self.maximum_readers = 0

    async def read(self, size=65536):
        self.readers += 1
        self.maximum_readers = max(self.maximum_readers, self.readers)
        try:
            return await self.queue.get()
        finally:
            self.readers -= 1

    async def write(self, data):
        self.sent.append((self.in_slave, data))
        if data.startswith(b'dbclient '):
            address = data.split(b'@', 1)[1].split(b' ', 1)[0]
            self.queue.put_nowait(b'admin@' + address + b"'s password: ")
        elif data == b'device-login\n':
            self.in_slave = True
            self.queue.put_nowait(f'BusyBox slave built-in shell ({self.advertised_mode})\n# '.encode())
        elif data == b'ls\n':
            self.queue.put_nowait(b"ls\n'ls' Not Supported, Try 'help'\n# " if self.mode == 'psh' else b'ls\nbin etc proc\n# ')
        elif data == b'debug\n':
            assert self.in_slave
            self.queue.put_nowait(CHALLENGE[:80].encode())
            self.queue.put_nowait(CHALLENGE[80:].encode() + b'\nPassword:')
        elif data == b'slave-debug-password\n':
            assert self.in_slave
            self.mode = 'ash'
            self.queue.put_nowait(b'BusyBox built-in shell (ash)\n# ')
            self.ash_ready.set()

    async def close(self):
        self.closed = True
        self.queue.put_nowait(b'')


@pytest.mark.parametrize('target', ['SLAVE_1', 'SLAVE_2', 'SLAVE_3'])
@pytest.mark.parametrize('entry', ['initial', 'manual', 'scheduled'])
@pytest.mark.parametrize('advertised_mode', ['psh', 'ash'])
async def test_slave_debug_all_entries_confirm_current_shell(tmp_path, entry, advertised_mode, target):
    """从机横幅或旧模式缓存不能代替当前探测，三种debug入口均须完成从机握手。"""
    task = {'id': 'slave-task', 'runId': 'run', 'protocol': 'SSH', 'sshTarget': target,
            'storageIdentity': 'synthetic-slave', 'initialCommands': [], 'scheduledCommands': []}
    if entry == 'initial':
        task['initialCommands'] = [{'command': 'debug', 'timeoutSeconds': 2}, {'command': 'after-debug'}]
    if entry == 'scheduled':
        task['scheduledCommands'] = [{'id': 'debug-timer', 'command': 'debug', 'intervalSeconds': .01,
                                      'totalExecutions': 1, 'timeoutSeconds': 2}]
    connection = NestedShell(advertised_mode=advertised_mode)
    resolved, events = [], []

    async def resolve(config, source):
        assert connection.in_slave
        assert config['sshTarget'] == target
        resolved.append(source)
        return 'slave-debug-password'

    async def factory(config):
        shell = ShellBootstrap(connection, config, resolve)
        await shell.enter_slave(config['sshTarget'], 'device-login')
        return shell

    collector = Collector(task, tmp_path, connection_factory=factory, resolve_debug_password=resolve,
                          on_debug=lambda event, _details: events.append(event))
    try:
        await collector.start()
        if entry == 'manual':
            await collector.enqueue_manual('debug')
        await asyncio.wait_for(connection.ash_ready.wait(), 2)
        async with asyncio.timeout(2):
            while 'ASH_READY' not in events:
                await asyncio.sleep(.01)
        assert resolved == [CHALLENGE]
        assert connection.maximum_readers == 1
        assert all(in_slave for in_slave, data in connection.sent if data in (b'debug\n', b'slave-debug-password\n'))
        if entry == 'initial':
            writes = [data for _in_slave, data in connection.sent]
            assert writes.index(b'slave-debug-password\n') < writes.index(b'after-debug\n')
    finally:
        await collector.stop()
    assert connection.closed


async def test_already_ash_slave_confirms_debug_without_decryption(tmp_path):
    """从机已在ASH时使用当前命令响应确认，不调用解密器或发送口令。"""
    connection = NestedShell(actual_mode='ash', advertised_mode='ash')
    original = connection.write

    async def write(data):
        if data == b'debug\n':
            connection.sent.append((connection.in_slave, data))
            connection.queue.put_nowait(b'-ash: debug: not found\n# ')
        else:
            await original(data)

    connection.write = write

    async def forbidden_provider(_task, _source):
        pytest.fail('ASH不应请求解密')

    task = {'id': 'ash-slave', 'runId': 'run', 'storageIdentity': 'synthetic-slave',
            'protocol': 'SSH', 'sshTarget': 'SLAVE_2', 'initialCommands': [{'command': 'debug'}]}

    async def factory(config):
        shell = ShellBootstrap(connection, config, forbidden_provider)
        return await shell.enter_slave(config['sshTarget'], 'device-login')

    collector = Collector(task, tmp_path, connection_factory=factory, resolve_debug_password=forbidden_provider)
    try:
        await collector.start()
        assert collector._commands._debug.mode == 'ASH'
        assert (True, b'debug\n') in connection.sent
        assert not any(data == b'slave-debug-password\n' for _slave, data in connection.sent)
    finally:
        await collector.stop()


async def test_slave_first_manual_debug_waits_for_delayed_banner_handoff(tmp_path):
    """横幅交接超过旧250ms窗口也先等reader确认，不能把它误当成debug的新响应。"""
    connection = NestedShell()
    task = {'id': 'delayed-slave', 'runId': 'run', 'protocol': 'SSH', 'sshTarget': 'SLAVE_3',
            'storageIdentity': 'synthetic-slave'}

    async def factory(config):
        shell = ShellBootstrap(connection, config)
        await shell.enter_slave(config['sshTarget'], 'device-login')
        original = shell.read
        first = True

        async def delayed_read(size=65536):
            nonlocal first
            if first:
                first = False
                await asyncio.sleep(.35)
            return await original(size)

        shell.read = delayed_read
        return shell

    collector = Collector(task, tmp_path, connection_factory=factory,
                          resolve_debug_password=lambda _task, _source: 'slave-debug-password')
    try:
        await collector.start()
        assert collector._commands._debug.mode == 'PSH'
        await collector.enqueue_manual('debug')
        assert connection.mode == 'ash'
    finally:
        await collector.stop()
