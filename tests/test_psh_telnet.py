"""真实 Telnet 串口 PSH 集成测试：syslog 穿插不得破坏握手或原始日志。"""

from __future__ import annotations

import asyncio
import base64
import re

import telnetlib3
from camera_logs.collection.collector import Collector
from camera_logs.collection.connections import connect

SOURCE = base64.b64encode(b"x" * 261).decode()
PASSWORD = "synthetic-telnet-debug-password"
SYSLOG = b"Sep  8 14:27:09 dsp.crit bscdsp: periodic\r\n"
PREFIX = re.compile(rb"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")


async def test_telnet_psh_syslog_interleaving_keeps_single_connection_and_raw_logs(tmp_path):
    """经真实 telnetlib3 连接验证旁路过滤仅影响识别窗口，绝不改写日志链路。"""
    commands, logs, handlers = [], [], set()
    peer_closed, next_received = asyncio.Event(), asyncio.Event()
    probes = 0

    async def write_fragments(writer, data):
        """按短片段发送，迫使 challenge、提示符和横幅经历跨 TCP 包重组。"""
        for start in range(0, len(data), 13):
            writer.write(data[start:start + 13])
            await writer.drain()
            await asyncio.sleep(0)

    async def serve(reader, writer):
        """模拟透明串口服务器，只对 debug、合成口令和 next 依次响应。"""
        nonlocal probes
        task = asyncio.current_task()
        handlers.add(task)
        buffer = b""
        try:
            while data := await reader.read(65536):
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    command = line.rstrip(b"\r")
                    commands.append(command)
                    if command == b"ls":
                        probes += 1
                        assert probes == 1
                        await write_fragments(writer, b"'ls' Not Supported, Try 'help'\r\n# ")
                    elif command == b"debug":
                        response = b"enc_string:\n" + SOURCE[:121].encode() + SYSLOG + SOURCE[121:].encode() + b"\nPass" + SYSLOG + b"word:"
                        await write_fragments(writer, response)
                    elif command == PASSWORD.encode():
                        response = b"BusyBox built-in shell (a" + SYSLOG + b"sh)\n# "
                        await write_fragments(writer, response)
                    elif command == b"next":
                        next_received.set()
        finally:
            writer.close()
            await writer.wait_closed()
            peer_closed.set()
            handlers.discard(task)

    server = await telnetlib3.create_server(host="127.0.0.1", port=0, shell=serve, encoding=False, connect_maxwait=.05, timeout=False)
    collector = None
    try:
        port = server.sockets[0].getsockname()[1]

        async def resolve(task, challenge):
            assert task["protocol"] == "TELNET_SERIAL"
            assert challenge == SOURCE
            return PASSWORD

        collector = Collector(
            {"id": "telnet-debug", "runId": "run", "storageIdentity": "testingdevice", "protocol": "TELNET_SERIAL", "ip": "127.0.0.1", "port": port,
             "pshSerialCharacterInterval": .001,
             "initialCommands": [{"command": "debug", "timeoutSeconds": 3}, {"command": "next"}], "scheduledCommands": []},
            tmp_path, connection_factory=connect, resolve_debug_password=resolve,
            on_log=lambda chunk: logs.append(chunk.data),
        )
        await asyncio.wait_for(collector.start(), 5)
        await asyncio.wait_for(next_received.wait(), 2)
        await collector.stop()
        await asyncio.wait_for(peer_closed.wait(), 2)

        assert commands == [b"ls", b"debug", PASSWORD.encode(), b"next"]
        assert len(handlers) == 0
        raw = PREFIX.sub(b"", b"".join(logs))
        assert raw.count(SYSLOG) == 3
        assert b"'ls' Not Supported, Try 'help'\r\n# " in raw
        assert b"enc_string:\n" + SOURCE[:121].encode() in raw
        assert SOURCE[121:].encode() + b"\nPass" in raw
        assert b"word:" in raw
        assert b"BusyBox built-in shell (a" in raw
        assert b"sh)\n# " in raw
    finally:
        if collector and not collector._connection_closed:
            await collector.stop()
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
