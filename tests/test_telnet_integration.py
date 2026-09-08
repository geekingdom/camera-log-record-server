"""使用真实 telnetlib3 和本地 TCP 服务验证保活控制字节及日志字节完整性。"""

import asyncio

import telnetlib3
from camera_logs.collection.collector import Collector
from camera_logs.collection.connections import _TelnetConnection


async def test_real_telnet_heartbeat_preserves_connection_and_raw_data():
    received = bytearray()
    nop_seen = asyncio.Event()
    peer_closed = asyncio.Event()
    commands_seen = asyncio.Event()
    handlers = set()

    async def serve(reader, writer):
        """模拟透明串口服务器，不把协议控制字节回显为设备正文。"""
        handlers.add(asyncio.current_task())
        try:
            writer.write(b"first\nsecond-\xff\xff\n")
            await writer.drain()
            while data := await reader.read(65536):
                received.extend(data)
                if b"\xff\xf1" in received:
                    nop_seen.set()
                if b"manual-command\n" in received:
                    commands_seen.set()
        finally:
            writer.close()
            await writer.wait_closed()
            peer_closed.set()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    connection = None
    try:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await telnetlib3.open_connection(
            "127.0.0.1", port, encoding=False, connect_maxwait=.05)
        connection = _TelnetConnection(reader, writer, interval=.02)
        await asyncio.wait_for(nop_seen.wait(), 1)
        assert not peer_closed.is_set()
        expected = b"first\nsecond-\xff\n"
        actual = bytearray()
        async with asyncio.timeout(1):
            while len(actual) < len(expected):
                chunk = await connection.read()
                assert chunk, "连接不应在正文读取完成前断开"
                actual.extend(chunk)
        assert actual == expected
        await connection.write(b"manual-command\n")
        await asyncio.wait_for(commands_seen.wait(), 1)
        assert b"\xff\xff\xf1" not in received
        await connection.close()
        await asyncio.wait_for(peer_closed.wait(), 1)
        assert connection._heartbeat.done()
    finally:
        if connection:
            await connection.close()
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


async def test_protocol_traffic_does_not_reset_default_ten_second_log_timeout(tmp_path):
    """对端持续回协议 NOP，仍必须按默认十秒无正文规则释放连接。"""
    peer_closed = asyncio.Event()
    handlers = set()
    states = []
    nop_count = 0

    async def serve(reader, writer):
        nonlocal nop_count
        handlers.add(asyncio.current_task())
        try:
            while True:
                writer.write(b"\xff\xf1")
                await writer.drain()
                nop_count += 1
                try:
                    if not await asyncio.wait_for(reader.read(65536), .1):
                        break
                except TimeoutError:
                    continue
        finally:
            writer.close()
            await writer.wait_closed()
            peer_closed.set()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    collector = None
    try:
        reader, writer = await telnetlib3.open_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1], encoding=False, connect_maxwait=.05)
        connection = _TelnetConnection(reader, writer, interval=.1)
        collector = Collector({"id": "silent", "runId": "run", "initialCommands": []},
            tmp_path, connection_factory=lambda _: connection,
            on_state=lambda state, _: states.append(state))
        started = asyncio.get_running_loop().time()
        await collector.start()
        await asyncio.wait_for(collector.wait_closed(), 15)
        assert asyncio.get_running_loop().time() - started >= 10
        assert "IDLE_TIMEOUT" in states
        assert nop_count > 1
        await asyncio.wait_for(peer_closed.wait(), 1)
        assert connection._heartbeat.done()
        assert not list(tmp_path.rglob("*.tar.gz"))
    finally:
        if collector:
            await collector.stop()
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
