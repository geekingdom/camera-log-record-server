"""本地 AsyncSSH 验证空闲重连使用生产 SSH 连接、初始化与同运行预算。"""

import asyncio
import time

import asyncssh
import pytest
from camera_logs.collection.connections import _connect_ssh
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

pytestmark = pytest.mark.usefixtures("mock_reservation_transaction")


def _command_lines(payload: bytearray) -> list[bytes]:
    """从 SSH stdin 累积字节复原完整命令，分包不影响初始化和预算断言。"""
    return [line.rstrip(b"\r") for line in bytes(payload).split(b"\n")[:-1]]


async def test_ssh_idle_reconnects_after_close_and_reuses_run_budget(tmp_path):
    """协议保活不算日志；关闭确认后重连，两会话各初始化一次且预算累计。"""
    authentication, payloads, process_handlers, server_closed, keepalive_connections = [], [], set(), set(), set()
    first_client_closed = asyncio.Event()
    second_factory_started = asyncio.Event()
    second_commands_sent = asyncio.Event()
    first_idle_timeout = asyncio.Event()
    connection_ready_at, first_idle_at = [], []
    connection_number = 0
    runtime = None

    class Server(asyncssh.SSHServer):
        """记录真实认证、协议保活及服务端连接关闭，不输出设备日志正文。"""

        def connection_made(self, connection):
            nonlocal connection_number
            connection_number += 1
            self.number = connection_number
            original = connection._process_keepalive_at_openssh_dot_com_global_request

            def observe_keepalive(packet):
                keepalive_connections.add(self.number)
                return original(packet)

            connection._process_keepalive_at_openssh_dot_com_global_request = observe_keepalive

        def begin_auth(self, _username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            authentication.append((username, password))
            return username == "collector" and password == "password"

        def connection_lost(self, _error):
            server_closed.add(self.number)

    async def silent_process(process):
        """从真实服务端 stdin 持续读取命令，不输出会刷新空闲计时的设备正文。"""
        handler = asyncio.current_task()
        assert handler is not None
        process_handlers.add(handler)
        payload = bytearray()
        payloads.append(payload)
        while chunk := await process.stdin.read(1):
            payload.extend(chunk.encode() if isinstance(chunk, str) else chunk)
            expected = {b"outputClose", b"outputOpen", b"setDebug -m all -l 7 -d 111", b"prtHardInfo", b"probe"}
            if len(payloads) == 2 and expected.issubset(_command_lines(payload)):
                second_commands_sent.set()

    server = await asyncssh.listen(
        "127.0.0.1", 0, server_factory=Server, process_factory=silent_process,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")], line_editor=False,
    )
    try:
        settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                            node_id="ssh-runtime-reconnect", start_background=False)
        repo = Repository(AsyncMongoMockClient().camera_logs, settings)
        await repo.initialize()
        task = {
            "id": "ssh-runtime-reconnect", "runId": "preserved-run", "nodeId": settings.node_id,
            "generation": 1, "status": "PENDING", "desiredState": "RUNNING", "protocol": "SSH",
            "ip": "127.0.0.1", "port": server.get_port(), "username": "collector",
            "passwordEncrypted": repo.encrypt("password"), "storageIdentity": "ssh-runtime",
            "initialCommands": [
                {"command": "outputClose", "delaySeconds": .3},
                {"command": "outputOpen", "delaySeconds": .3},
                {"command": "setDebug -m all -l 7 -d 111", "delaySeconds": .3},
                {"command": "prtHardInfo", "delaySeconds": .3},
            ],
            "scheduledCommands": [{"id": "periodic", "command": "probe", "totalExecutions": 2,
                                   "intervalSeconds": 6}],
        }
        await repo.db.tasks.insert_one(task.copy())
        factory_calls = 0

        async def factory(config):
            """确认第一客户端 close 完成后才允许第二次真实 SSH 连接。"""
            nonlocal factory_calls
            factory_calls += 1
            number = factory_calls
            if number == 2:
                assert first_client_closed.is_set()
                second_factory_started.set()
            connection = await _connect_ssh(config, config["ip"], config["port"])
            # 以生产连接完成认证和 shell 创建的时刻为基准，初始化延时不能缩短
            # 默认十秒无设备正文看门狗的实际验收窗口。
            connection_ready_at.append(time.monotonic())
            # 仅测试连接缩短协议保活，实际生产连接保留 15 秒默认值。
            connection._client.set_keepalive(interval=.2, count_max=3)
            original_close = connection.close

            async def close():
                await original_close()
                if number == 1:
                    first_client_closed.set()

            connection.close = close
            return connection

        started = time.monotonic()
        runtime = SessionRuntime(repo, task, connection_factory=factory)
        original_on_state = runtime.on_state

        async def on_state(state, details):
            """测量首个真实会话的空闲状态，而不是用整体测试耗时替代。"""
            await original_on_state(state, details)
            if state == "IDLE_TIMEOUT" and not first_idle_timeout.is_set():
                first_idle_at.append(time.monotonic())
                first_idle_timeout.set()

        runtime.on_state = on_state
        await asyncio.wait_for(first_idle_timeout.wait(), timeout=16)
        assert len(connection_ready_at) == 1
        assert 9.99 <= first_idle_at[0] - connection_ready_at[0] < 13
        await asyncio.wait_for(second_factory_started.wait(), timeout=5)
        await asyncio.wait_for(second_commands_sent.wait(), timeout=9)
        await runtime.stop()
        await asyncio.wait_for(
            _wait_until(lambda: server_closed == {1, 2}), timeout=3,
        )

        commands = [item async for item in repo.db.commands.find({"kind": "SCHEDULED"})]
        budget = await repo.db.budgets.find_one({"_id": "preserved-run:periodic"})
        assert time.monotonic() - started >= 10
        assert factory_calls == 2
        assert authentication == [("collector", "password"), ("collector", "password")]
        assert keepalive_connections == {1, 2}
        assert [_command_lines(payload) for payload in payloads] == [
            [b"outputClose", b"outputOpen", b"setDebug -m all -l 7 -d 111", b"prtHardInfo", b"probe"],
            [b"outputClose", b"outputOpen", b"setDebug -m all -l 7 -d 111", b"prtHardInfo", b"probe"],
        ]
        assert budget["attempts"] == 2
        assert len({item["sessionId"] for item in commands}) == 2
        assert [item["attempt"] for item in sorted(commands, key=lambda item: item["attempt"])] == [1, 2]
        assert {item["status"] for item in commands} == {"SENT"}
    finally:
        if runtime is not None:
            await runtime.stop()
        for handler in process_handlers:
            handler.cancel()
        await asyncio.gather(*process_handlers, return_exceptions=True)
        server.close()
        await server.wait_closed()


async def _wait_until(predicate):
    """以短轮询等待服务器异步关闭回调，避免测试依赖固定调度时序。"""
    while not predicate():
        await asyncio.sleep(.01)
