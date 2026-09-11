"""Telnet 协商尚未返回 writer 时的超时、取消和收尾回归。"""

from __future__ import annotations

import asyncio

import pytest
from camera_logs.collection import connections
from camera_logs.collection.connections import _cleanup_failed_telnet_connection, _connect_telnet
from camera_logs.collection.telnet_client import LogTelnetClient


async def _waiting_server(monkeypatch):
    """返回保持协商未完成的客户端工厂及服务端 EOF 观察点。"""
    connected, peer_eof = asyncio.Event(), asyncio.Event()
    peers, handlers = set(), set()

    class WaitingClient(LogTelnetClient):
        def connection_made(self, transport):
            super().connection_made(transport)
            connected.set()

        def check_negotiation(self, final=False):
            return False

    async def serve(reader, writer):
        peers.add(writer)
        handler = asyncio.current_task()
        handlers.add(handler)
        try:
            while await reader.read(65536):
                pass
            peer_eof.set()
        finally:
            writer.close()
            await writer.wait_closed()
            peers.discard(writer)
            handlers.discard(handler)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    monkeypatch.setattr(connections, "_log_telnet_client", lambda **kwargs: WaitingClient(**kwargs))

    async def close_server():
        # 即使待测代码泄漏连接，测试也必须释放服务端，不能在wait_closed中永久等待。
        server.close()
        for writer in tuple(peers):
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in tuple(peers)))
        await server.wait_closed()
        await asyncio.gather(*tuple(handlers), return_exceptions=True)

    return server, connected, peer_eof, close_server


async def test_telnet_negotiation_cancellation_closes_connected_socket(monkeypatch):
    """TCP 已接通但协商未返回 writer 时，取消仍让服务端收到 EOF。"""
    server, connected, peer_eof, close_server = await _waiting_server(monkeypatch)
    try:
        task = asyncio.create_task(_connect_telnet({}, "127.0.0.1", server.sockets[0].getsockname()[1]))
        await asyncio.wait_for(connected.wait(), 1)
        # 让库完成create_connection并进入协商等待；在connection_made当拍取消只覆盖TCP建连取消。
        await asyncio.sleep(.05)
        assert not task.done(), "取消前必须仍停留在协议协商阶段"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(peer_eof.wait(), 1)
    finally:
        await close_server()


async def test_telnet_negotiation_timeout_closes_connected_socket(monkeypatch):
    """缩短建连阈值时，协商阶段超时也不能遗留服务端可见 TCP。"""
    monkeypatch.setattr(connections, "TELNET_CONNECT_TIMEOUT_SECONDS", .1)
    server, connected, peer_eof, close_server = await _waiting_server(monkeypatch)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await _connect_telnet({}, "127.0.0.1", server.sockets[0].getsockname()[1])
        await asyncio.wait_for(connected.wait(), 1)
        await asyncio.wait_for(peer_eof.wait(), 1)
    finally:
        await close_server()


async def test_second_cancellation_aborts_hanging_telnet_cleanup(monkeypatch):
    """二次取消主动 abort，后台有界收尾自行消费异常而不泄漏任务告警。"""
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()

    def capture_unhandled(current_loop, context):
        unhandled.append(context)
        if previous_handler is not None:
            previous_handler(current_loop, context)

    loop.set_exception_handler(capture_unhandled)

    class Writer:
        def __init__(self):
            self.waiting = asyncio.Event()
            self.aborted = False

        def close(self):
            return None

        def abort(self):
            self.aborted = True

        async def wait_closed(self):
            self.waiting.set()
            await asyncio.Future()

    try:
        monkeypatch.setattr(connections, "CLOSE_TIMEOUT_SECONDS", .01)
        writer = Writer()
        cleanup = asyncio.create_task(_cleanup_failed_telnet_connection(writer, asyncio.CancelledError()))
        await writer.waiting.wait()
        cleanup.cancel()
        await cleanup
        assert writer.aborted
        await asyncio.sleep(.02)
        assert not unhandled
    finally:
        loop.set_exception_handler(previous_handler)
