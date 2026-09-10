"""Telnet 协议保活异常的关闭与运行时重连回归。"""

from __future__ import annotations

import asyncio
import logging

from camera_logs.collection.connections import _connect_telnet, _TelnetConnection
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def test_close_cancels_heartbeat_when_its_drain_is_blocked():
    """停止发生在保活背压等待中时，取消必须先收敛 heartbeat 再关闭传输。"""
    class Writer:
        def __init__(self):
            self.entered = asyncio.Event()
            self.closed = False

        def get_extra_info(self, _key):
            return None

        def send_iac(self, _data):
            return None

        async def drain(self):
            self.entered.set()
            await asyncio.Future()

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    writer = Writer()
    connection = _TelnetConnection(None, writer, interval=.001)
    try:
        await asyncio.wait_for(writer.entered.wait(), 1)
        await asyncio.wait_for(connection.close(), .1)

        assert writer.closed
        assert connection._heartbeat.done() and connection._heartbeat.cancelled()
    finally:
        if not connection._heartbeat.done():
            await asyncio.wait_for(connection.close(), .1)


async def test_heartbeat_drain_failure_closes_real_socket_and_reconnects_runtime(tmp_path, monkeypatch, caplog):
    """heartbeat 的连接错误必须有日志、触发真实 EOF，并让运行时建立下一会话。"""
    handlers, all_eofs, second_collecting = set(), asyncio.Event(), asyncio.Event()
    connection_number, factory_calls, states, eof_numbers = 0, 0, [], set()
    reconnecting_after_close = None

    async def serve(reader, writer):
        """本机 TCP 服务只观测客户端实际关闭，不制造客户端 reader 的 EOF。"""
        nonlocal connection_number
        connection_number += 1
        number = connection_number
        handler = asyncio.current_task()
        handlers.add(handler)
        try:
            while await reader.read(65536):
                pass
            eof_numbers.add(number)
            if eof_numbers == {1, 2}:
                all_eofs.set()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(handler)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    runtime = None
    try:
        settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                            node_id="heartbeat-failure-node", start_background=False)
        repo = Repository(AsyncMongoMockClient().camera_logs, settings)
        await repo.initialize()
        task = {
            "id": "heartbeat-failure", "runId": "run", "nodeId": settings.node_id, "generation": 1,
            "status": "PENDING", "desiredState": "RUNNING", "protocol": "TELNET_DEVICE", "ip": "127.0.0.1",
            "port": server.sockets[0].getsockname()[1], "passwordEncrypted": "", "storageIdentity": "telnet",
            "initialCommands": [], "scheduledCommands": [], "telnetKeepaliveInterval": .001,
        }
        await repo.db.tasks.insert_one(task.copy())

        async def factory(config):
            """第一连接只让 heartbeat 写失败；第二连接保持正常以证明重连完成。"""
            nonlocal factory_calls
            factory_calls += 1
            connection = await _connect_telnet(config, config["ip"], config["port"])
            if factory_calls == 1:
                async def fail_heartbeat_drain():
                    raise ConnectionResetError("heartbeat drain reset")

                connection._writer.drain = fail_heartbeat_drain
            return connection

        monkeypatch.setattr("random.random", lambda: 0)
        caplog.set_level(logging.ERROR, logger="camera_logs.collection.connections")
        runtime = SessionRuntime(repo, task, connection_factory=factory)
        original_on_state = runtime.on_state

        async def on_state(state, details):
            nonlocal reconnecting_after_close
            states.append(state)
            await original_on_state(state, details)
            if state == "CLOSED":
                reconnecting_after_close = (await repo.db.tasks.find_one({"id": task["id"]}))["status"]
            if state == "COLLECTING" and factory_calls == 2:
                second_collecting.set()

        runtime.on_state = on_state
        await asyncio.wait_for(second_collecting.wait(), 3)

        assert factory_calls == 2
        assert "CLOSED" in states
        assert reconnecting_after_close == "RECONNECTING"
        assert any("Telnet 协议保活失败" in record.message for record in caplog.records)
        assert await repo.db.events.find_one({"taskId": task["id"], "type": "CONNECTION_GAP"})
        await runtime.stop()
        await asyncio.wait_for(all_eofs.wait(), 2)
        assert eof_numbers == {1, 2}
    finally:
        if runtime:
            await runtime.stop()
        server.close()
        await server.wait_closed()
        for handler in list(handlers):
            handler.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
