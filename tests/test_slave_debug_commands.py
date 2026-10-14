"""从机切换后的调试入口回归：复用主机解密链路，不依赖公司实机或解密服务。"""

import asyncio
import base64
import re

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
        self.target = 'SLAVE_1'
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
            self.target = {b'192.168.253.164': 'SLAVE_1', b'192.168.253.166': 'SLAVE_2',
                           b'192.168.253.167': 'SLAVE_3'}[address]
            self.queue.put_nowait(b'admin@' + address + b"'s password: ")
        elif data == b'device-login\n':
            self.in_slave = True
            self.queue.put_nowait(f'BusyBox slave built-in shell ({self.advertised_mode})\n# '.encode())
        elif data == b'ls\n':
            self.queue.put_nowait(b"ls\n'ls' Not Supported, Try 'help'\n# " if self.mode == 'psh' else b'ls\nbin etc proc\n# ')
        elif data == b'debug\n':
            assert self.in_slave
            if self.mode == 'ash':
                self.queue.put_nowait(b'-ash: debug: not found\n# ')
            else:
                self.queue.put_nowait(CHALLENGE[:80].encode())
                self.queue.put_nowait(CHALLENGE[80:].encode() + b'\nPassword:')
        elif data == b'slave-debug-password\n':
            assert self.in_slave
            self.mode = 'ash'
            self.queue.put_nowait(b'BusyBox built-in shell (ash)\n# ')
            self.ash_ready.set()
        elif data.startswith(b'ifconfig;'):
            marker = re.search(rb'(__SLAVE_BOOT_[0-9a-f]+):%s', data).group(1)
            addresses = {'SLAVE_1': b'192.168.253.164', 'SLAVE_2': b'192.168.253.166', 'SLAVE_3': b'192.168.253.167'}
            self.queue.put_nowait(b'eth0\n inet addr:' + addresses.get(self.target, addresses['SLAVE_1']) + b'\n')
            self.queue.put_nowait(b'\n' + marker + b':0\n')

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
            while sum(data == b'debug\n' for _, data in connection.sent) < 2:
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
        shell = ShellBootstrap(connection, config, lambda _task, _source: 'slave-debug-password')
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
        assert collector._commands._debug.mode == 'ASH'
        await collector.enqueue_manual('debug')
        assert connection.mode == 'ash'
    finally:
        await collector.stop()


@pytest.mark.parametrize('entry', ['initial', 'manual', 'scheduled'])
async def test_failed_slave_debug_closes_instead_of_recovering_parent(tmp_path, entry):
    """从机调试失败不能发送Ctrl-C恢复到父PSH，更不能继续初始化/采集父日志。"""
    connection = NestedShell()
    task = {'id': 'strict-slave', 'runId': 'run', 'storageIdentity': 'test', 'protocol': 'SSH',
            'sshTarget': 'SLAVE_1', 'initialCommands': [], 'scheduledCommands': []}
    if entry == 'initial':
        task['initialCommands'] = [{'command': 'debug', 'timeoutSeconds': .05}, {'command': 'must-not-send'}]
    if entry == 'scheduled':
        task['scheduledCommands'] = [{'id': 'debug', 'command': 'debug', 'intervalSeconds': .01,
                                      'totalExecutions': 1, 'timeoutSeconds': .05}]

    async def factory(config):
        shell = ShellBootstrap(connection, config, lambda *_: 'slave-debug-password')
        await shell.enter_slave('SLAVE_1', 'device-login')
        original = connection.write

        async def silent_debug(data):
            if data == b'debug\n':
                connection.sent.append((True, data))
            else:
                await original(data)
        connection.write = silent_debug
        return shell

    collector = Collector(task, tmp_path, connection_factory=factory)
    try:
        if entry == 'initial':
            with pytest.raises(ConnectionError):
                await collector.start()
        else:
            await collector.start()
            if entry == 'manual':
                debug = asyncio.create_task(collector.enqueue_manual('debug', timeout_seconds=.05))
                await asyncio.sleep(.01)
                queued = asyncio.create_task(collector.enqueue_manual('must-not-send'))
                results = await asyncio.wait_for(asyncio.gather(debug, queued, return_exceptions=True), 2)
                assert all(isinstance(result, ConnectionError) for result in results)
            else:
                async with asyncio.timeout(2):
                    while not connection.closed:
                        await asyncio.sleep(.01)
        assert connection.closed
        assert not any(b'\x03' in data or b'must-not-send' in data for _, data in connection.sent)
    finally:
        await collector.stop()


async def test_failed_slave_close_preserves_uncertain_transport_error(tmp_path):
    """严格终止沿用Collector关闭锁，失败不确认transport释放并保留原异常。"""
    from unittest.mock import AsyncMock
    collector = Collector({'id': 'uncertain', 'runId': 'run', 'storageIdentity': 'test'}, tmp_path, connection_factory=AsyncMock())
    error = ConnectionError('transport close uncertain')
    connection = type('Connection', (), {'close': AsyncMock(side_effect=error)})()
    collector._connection = connection
    await collector._close_failed_slave()
    assert collector._terminal_error is error
    assert not collector._connection_closed
    assert not collector._accepting_commands
    with pytest.raises(ConnectionError):
        await collector.stop()


async def test_failed_slave_debug_with_transport_close_error_still_closes_collector(tmp_path):
    """真实从机包装先终止唯一读取者，底层关闭失败也不能让收尾或父机输出悬挂。"""
    connection = NestedShell()
    original_close = connection.close
    task = {'id': 'close-error-slave', 'runId': 'run', 'storageIdentity': 'test', 'protocol': 'SSH',
            'sshTarget': 'SLAVE_1', 'initialCommands': [], 'scheduledCommands': []}

    async def factory(config):
        shell = ShellBootstrap(connection, config, lambda *_: 'slave-debug-password')
        await shell.enter_slave('SLAVE_1', 'device-login')
        original_write = connection.write

        async def silent_debug(data):
            if data == b'debug\n':
                connection.sent.append((True, data))
            else:
                await original_write(data)

        async def close_after_reader_shutdown():
            raise ConnectionError('synthetic transport close uncertainty')

        connection.write = silent_debug
        connection.close = close_after_reader_shutdown
        return shell

    collector = Collector(task, tmp_path, connection_factory=factory)
    try:
        await collector.start()
        with pytest.raises(ConnectionError):
            await collector.enqueue_manual('debug', timeout_seconds=.05)
        await asyncio.wait_for(collector.wait_closed(), 1)
        assert collector._terminal_error is not None
        assert not connection.closed
        connection.close = original_close
        await asyncio.gather(collector._reader, return_exceptions=True)
    finally:
        connection.close = original_close
        try:
            await collector.stop()
        except ConnectionError:
            pass
