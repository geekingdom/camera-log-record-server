"""协议级初始化：主机尽力切换，从机必须有明确身份凭证，串口保持原样。"""

import asyncio
import base64

import pytest
from camera_logs.collection.collector import Collector


class Device:
    """带真实命令交互的单接收流，失败时可返回PSH并继续普通命令。"""

    def __init__(self, mode='psh'):
        self.sent = []
        self.closed = False
        self.queue = asyncio.Queue()
        self.queue.put_nowait(f'BusyBox built-in shell ({mode})\n# '.encode())

    async def read(self, size=65536):
        return await self.queue.get()

    async def write(self, data):
        self.sent.append(data)
        if data == b'debug\n':
            self.queue.put_nowait(base64.b64encode(b'challenge' * 20) + b'\nPassword:')
        elif data == b'approved\n':
            self.queue.put_nowait(b'BusyBox built-in shell (ash)\n# ')
        elif data == b'\x03':
            self.queue.put_nowait(b'\n# ')
        elif data == b'ls\n':
            self.queue.put_nowait(b"ls\n'ls' Not Supported, Try 'help'\n# ")
        else:
            self.queue.put_nowait(b'normal device output\n# ')

    async def close(self):
        self.closed = True


@pytest.mark.parametrize('protocol', ['SSH', 'TELNET_DEVICE'])
@pytest.mark.parametrize('success', [True, False])
async def test_network_automatic_ash_before_initial_but_failure_continues(tmp_path, protocol, success):
    device = Device()
    events = []

    def provider(_task, _source):
        if not success:
            raise RuntimeError('unavailable')
        return 'approved'

    collector = Collector({'storageIdentity': 'synthetic-bootstrap', 'id': 'network', 'protocol': protocol, 'initialCommands': [{'command': 'user-init'}]},
                          tmp_path, connection_factory=lambda _: device, resolve_debug_password=provider,
                          on_debug=lambda event, details: events.append(event))
    try:
        await collector.start()
        assert b'debug\n' in device.sent
        assert device.sent.index(b'debug\n') < device.sent.index(b'user-init\n')
        assert ('ASH_READY' if success else 'FAILED') in events
        assert not device.closed
        await collector.enqueue_manual('after-start')
        assert b'after-start\n' in device.sent
    finally:
        await collector.stop()
    assert device.closed


@pytest.mark.parametrize('protocol', ['SSH', 'TELNET_DEVICE', 'TELNET_SERIAL'])
async def test_ash_or_serial_does_not_send_unsolicited_debug(tmp_path, protocol):
    device = Device('psh' if protocol == 'TELNET_SERIAL' else 'ash')
    collector = Collector({'storageIdentity': 'synthetic-bootstrap', 'id': 'simple', 'protocol': protocol, 'initialCommands': [{'command': 'user-init'}]},
                          tmp_path, connection_factory=lambda _: device)
    try:
        await collector.start()
        assert device.sent == [b'user-init\n']
    finally:
        await collector.stop()


async def test_unverified_slave_never_executes_initial_or_collects(tmp_path):
    device = Device('ash')
    chunks = []
    collector = Collector({'storageIdentity': 'synthetic-bootstrap', 'id': 'slave', 'protocol': 'SSH', 'sshTarget': 'SLAVE_1',
                           'initialCommands': [{'command': 'user-init'}]}, tmp_path,
                          connection_factory=lambda _: device, on_log=lambda chunk: chunks.append(chunk))
    with pytest.raises(ConnectionError, match='从机'):
        await collector.start()
    assert device.closed
    assert not device.sent
    assert not chunks


async def test_automatic_handshake_uses_own_timeout_not_log_idle_timeout(tmp_path):
    """解密期间还未开始普通采集，不应被日志静默看门狗提前断开。"""
    device = Device()

    async def provider(_task, _source):
        await asyncio.sleep(.25)
        return 'approved'

    collector = Collector({'storageIdentity': 'synthetic-bootstrap', 'id': 'slow-provider', 'protocol': 'SSH',
                           'logIdleTimeoutSeconds': .1, 'initialCommands': [{'command': 'user-init'}]},
                          tmp_path, connection_factory=lambda _: device, resolve_debug_password=provider)
    try:
        await asyncio.wait_for(collector.start(), 2)
        assert b'approved\n' in device.sent and b'user-init\n' in device.sent
        assert not device.closed
    finally:
        await collector.stop()


@pytest.mark.parametrize('protocol', ['SSH', 'TELNET_DEVICE'])
async def test_network_without_user_initial_commands_still_enters_ash(tmp_path, protocol):
    """自动模式切换属于协议初始化，不依赖模板或用户debug命令。"""
    device = Device()
    collector = Collector({'storageIdentity': 'synthetic-bootstrap', 'id': 'empty-template', 'protocol': protocol},
                          tmp_path, connection_factory=lambda _: device,
                          resolve_debug_password=lambda _task, _source: 'approved')
    try:
        await collector.start()
        assert device.sent == [b'debug\n', b'approved\n']
    finally:
        await collector.stop()
