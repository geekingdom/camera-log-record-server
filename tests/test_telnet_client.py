"""优化接收与原库逐块差分，覆盖控制序列、分包和模式变更。"""

import asyncio
import random

import pytest
from camera_logs.collection.telnet_client import LogTelnetClient
from telnetlib3 import TelnetClient
from telnetlib3.slc import LMODE_MODE_REMOTE, SLC, SLC_IP, SLC_VARIABLE, Linemode
from telnetlib3.stream_writer import TelnetWriter
from telnetlib3.telopt import ECHO, LINEMODE, SGA


class Transport(asyncio.Transport):
    """记录协商回包，不访问真实设备。"""

    def __init__(self):
        self.output = bytearray()

    def write(self, data):
        self.output.extend(data)

    def is_closing(self):
        return False


class Reader:
    """只累计接收正文，差分不依赖分块边界。"""

    def __init__(self):
        self.data = bytearray()

    def feed_data(self, data):
        self.data.extend(data)


def make_client(cls, mode):
    """装配真实解析器与隔离传输，避免自动协商定时器影响断言。"""
    client = cls(encoding=False)
    client.reader = Reader()
    client.writer = TelnetWriter(Transport(), client, client=True)
    if mode == "kludge":
        client.writer.remote_option[ECHO] = True
        client.writer.remote_option[SGA] = True
    elif mode == "remote":
        client.writer.remote_option[LINEMODE] = True
        client.writer._linemode = Linemode(LMODE_MODE_REMOTE)
    return client


def close_client(client):
    """释放未运行网络连接使用的 Future。"""
    client.waiter_closed.cancel()
    client._waiter_connected.cancel()


async def test_ordinary_kludge_chunk_bypasses_python_byte_scanner(monkeypatch):
    client = make_client(LogTelnetClient, "kludge")

    def unexpected(*args):
        pytest.fail("普通日志仍进入原库逐字节扫描")

    monkeypatch.setattr(TelnetClient, "_process_chunk", unexpected)
    try:
        payload = b"\x1b[31mdevice log\x1b[0m\r\n" * 4096
        assert client._process_chunk(payload) is False
        assert client.reader.data == payload
        assert client._last_received is not None
    finally:
        close_client(client)


@pytest.mark.parametrize("mode", ["local", "kludge", "remote"])
@pytest.mark.parametrize("split", [1, 2, 7, 1024])
async def test_fragmented_controls_match_original_parser(mode, split):
    original = make_client(TelnetClient, mode)
    optimized = make_client(LogTelnetClient, mode)
    rng = random.Random(71)
    data = (b"first\n\xff\xfb\x01\xff\xfb\x03" + b"\xff\xffescaped\n"
            + b"\xff\xfa\x18\x01\xff\xf0" + bytes(range(255))
            + rng.randbytes(8192) + b"last\n")
    try:
        for offset in range(0, len(data), split):
            chunk = data[offset:offset + split]
            assert optimized._process_chunk(chunk) == original._process_chunk(chunk)
            assert optimized.reader.data == original.reader.data
            assert optimized.writer._transport.output == original.writer._transport.output
            assert optimized.writer.is_oob == original.writer.is_oob
            assert optimized.writer.mode == original.writer.mode
    finally:
        close_client(original)
        close_client(optimized)


@pytest.mark.parametrize("mode", ["kludge", "remote"])
async def test_dynamic_slc_table_callbacks_and_mode_changes_match_original(mode):
    original = make_client(TelnetClient, mode)
    optimized = make_client(LogTelnetClient, mode)
    calls = [[], []]
    try:
        for client, captured in zip((original, optimized), calls, strict=True):
            client.writer.set_slc_callback(SLC_IP, captured.append)
        for trigger in (b"Q", b"]", b"-", b"^", b"\\", b"\x00", b"\xff"):
            for client in (original, optimized):
                client._process_chunk(b"ordinary\n")
                # 使用独立定义，避免原库默认 SLC 对象被两个客户端共享。
                client.writer.slctab[SLC_IP] = SLC(SLC_VARIABLE, trigger)
            chunk = b"before" + trigger + b"after\n"
            assert optimized._process_chunk(chunk) == original._process_chunk(chunk)
            assert calls[0] == calls[1]
            assert optimized.reader.data == original.reader.data
        for client in (original, optimized):
            client.writer.remote_option[LINEMODE] = False
            client.writer.remote_option[ECHO] = False
            client.writer.slc_simulated = False
        assert optimized._process_chunk(b"local\x03\n") == original._process_chunk(b"local\x03\n")
        assert optimized.reader.data == original.reader.data
        assert calls[0]
    finally:
        close_client(original)
        close_client(optimized)


async def test_bulk_feed_error_is_not_retried(monkeypatch):
    client = make_client(LogTelnetClient, "kludge")
    attempts = []

    def fail(data):
        attempts.append(data)
        raise OSError("injected receive failure")

    monkeypatch.setattr(client.reader, "feed_data", fail)
    try:
        with pytest.raises(OSError, match="injected receive failure"):
            client._process_chunk(b"ordinary\n")
        assert attempts == [b"ordinary\n"]
    finally:
        close_client(client)
