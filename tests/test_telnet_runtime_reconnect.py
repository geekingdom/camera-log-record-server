"""真实 Telnet 连接在无设备正文时重连的运行时回归。"""

import asyncio
import time

import pytest
import telnetlib3
from camera_logs.collection.connections import _connect_telnet
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

pytestmark = pytest.mark.usefixtures("mock_reservation_transaction")


def _command_lines(payload):
    """按换行重组同一 TCP 连接收到的命令，分包不能改变命令计数。"""
    return [line.rstrip(b"\r") for line in payload.split(b"\n")[:-1]]


@pytest.mark.parametrize("protocol", ["TELNET_DEVICE", "TELNET_SERIAL"])
async def test_telnet_idle_reconnects_after_client_close_and_reuses_run_budget(tmp_path, protocol):
    """协议 NOP 不算正文；旧 Telnet 关闭完成后才建新会话，预算按运行累计。"""
    handlers, server_eof, received, server_eofs = set(), asyncio.Event(), [], set()
    logins = []
    initial_commands = ["outputClose", "outputOpen", "setDebug -m all -l 7 -d 111", "prtHardInfo"]
    client_heartbeats = set()
    client_close_finished, second_factory = asyncio.Event(), asyncio.Event()
    second_initialised, scheduled_twice = asyncio.Event(), asyncio.Event()
    client_close_elapsed, states = {}, []
    last_body_at, idle_elapsed = {}, []

    async def serve(reader, writer):
        """设备需要真实登录，串口无需登录；仅发送协议 NOP 而不回显命令正文。"""
        received.append(bytearray())
        connection_number = len(received)
        payload = received[-1]
        handlers.add(asyncio.current_task())
        try:
            writer.set_iac_callback(b"\xf1", lambda _command: client_heartbeats.add(connection_number))
            if protocol == "TELNET_DEVICE":
                writer.write(b"login:")
                await writer.drain()
                username = (await reader.readuntil(b"\n")).rstrip(b"\r\n")
                writer.write(b"Password:")
                await writer.drain()
                password = (await reader.readuntil(b"\n")).rstrip(b"\r\n")
                logins.append((connection_number, username, password))
            while True:
                writer.send_iac(b"\xff\xf1")
                await writer.drain()
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), .1)
                except TimeoutError:
                    continue
                if not chunk:
                    server_eofs.add(connection_number)
                    if server_eofs == {1, 2}:
                        server_eof.set()
                    return
                payload.extend(chunk)
                commands = _command_lines(payload)
                if connection_number == 2 and b"prtHardInfo" in commands:
                    second_initialised.set()
                # 每条命令可跨 TCP 分包，因此只按每个连接的累计字节判断。
                if len(received) >= 2 and all(b"probe" in _command_lines(item) for item in received[:2]):
                    scheduled_twice.set()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await telnetlib3.create_server(
        host="127.0.0.1", port=0, shell=serve, encoding=False, connect_maxwait=.05,
    )
    runtime = None
    try:
        settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                            node_id="telnet-reconnect-node", start_background=False)
        repo = Repository(AsyncMongoMockClient().camera_logs, settings)
        await repo.initialize()
        port = server.sockets[0].getsockname()[1]
        task = {
            "id": "telnet-reconnect", "runId": "preserved-run", "nodeId": settings.node_id, "generation": 1,
            "status": "PENDING", "desiredState": "RUNNING", "protocol": protocol, "ip": "127.0.0.1",
            "port": port, "username": "operator" if protocol == "TELNET_DEVICE" else "",
            "passwordEncrypted": repo.encrypt("secret" if protocol == "TELNET_DEVICE" else ""),
            # 仅缩短本测试连接的保活间隔，真实十秒无正文超时不加速。
            "telnetKeepaliveInterval": .2,
            "storageIdentity": "telnetdevice", "initialCommands": [
                {"command": command, "delaySeconds": .3} for command in initial_commands
            ], "scheduledCommands": [
                {"id": "periodic", "command": "probe", "totalExecutions": 2, "intervalSeconds": 6},
            ],
        }
        await repo.db.tasks.insert_one(task.copy())
        factory_calls = 0

        async def factory(config):
            """第二次连接前验证首个客户端 close 已完整返回，不能靠服务端调度顺序。"""
            nonlocal factory_calls
            factory_calls += 1
            connection_number = factory_calls
            if connection_number == 2:
                assert client_close_finished.is_set()
                second_factory.set()
            opened_at = time.monotonic()
            connection = await _connect_telnet(config, config["ip"], config["port"])
            last_body_at[connection_number] = time.monotonic()
            original_close = connection.close
            original_read = connection.read

            async def read(size=65536):
                """只观察生产解码后真实正文，保活控制帧不能更新最后正文时间。"""
                data = await original_read(size)
                if data:
                    last_body_at[connection_number] = time.monotonic()
                return data

            async def close():
                await original_close()
                client_close_elapsed[connection_number] = time.monotonic() - opened_at
                if connection_number == 1:
                    client_close_finished.set()

            connection.close = close
            connection.read = read
            return connection

        runtime = SessionRuntime(repo, task, connection_factory=factory)
        original_on_state = runtime.on_state

        async def on_state(state, details):
            states.append(state)
            if state == "IDLE_TIMEOUT":
                idle_elapsed.append(time.monotonic() - last_body_at[factory_calls])
            await original_on_state(state, details)

        runtime.on_state = on_state
        await asyncio.wait_for(second_factory.wait(), 20)
        await asyncio.wait_for(second_initialised.wait(), 5)
        await asyncio.wait_for(scheduled_twice.wait(), 9)
        await runtime.stop()
        await asyncio.wait_for(server_eof.wait(), 2)

        commands = [item async for item in repo.db.commands.find({"kind": "SCHEDULED"})]
        budget = await repo.db.budgets.find_one({"_id": "preserved-run:periodic"})
        payloads = [bytes(item) for item in received]
        assert factory_calls == 2
        assert client_close_elapsed[1] >= 10
        assert "IDLE_TIMEOUT" in states
        # 允许繁忙 CI 的调度余量，但默认退化到 15 秒或过早触发均应失败。
        assert len(idle_elapsed) == 1 and 9.99 <= idle_elapsed[0] < 13
        expected_commands = [command.encode() for command in initial_commands] + [b"probe"]
        assert [_command_lines(payload) for payload in payloads] == [expected_commands, expected_commands]
        expected_logins = [(1, b"operator", b"secret"), (2, b"operator", b"secret")]
        assert logins == (expected_logins if protocol == "TELNET_DEVICE" else [])
        assert client_heartbeats == {1, 2}
        assert budget["attempts"] == 2
        assert len({item["sessionId"] for item in commands}) == 2
        assert [item["attempt"] for item in sorted(commands, key=lambda item: item["attempt"])] == [1, 2]
        assert {item["status"] for item in commands} == {"SENT"}
        assert server_eofs == {1, 2}
    finally:
        if runtime:
            await runtime.stop()
        server.close()
        await server.wait_closed()
        for handler in list(handlers):
            handler.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
