"""使用本地 AsyncSSH 服务验证协议保活不延长采集空闲超时。"""

import asyncio

import asyncssh
import camera_logs.collection.collector as collector_module
import pytest
from camera_logs.collection.collector import Collector
from camera_logs.collection.connections import _connect_ssh


async def test_ssh_keepalive_does_not_reset_default_ten_second_log_timeout(tmp_path):
    """服务端仅响应 SSH 保活请求而不输出正文，采集器仍在十秒后关闭。"""
    keepalive_received = asyncio.Event()
    peer_closed = asyncio.Event()
    collector = None
    connection = None

    class Server(asyncssh.SSHServer):
        """记录 AsyncSSH 内建保活全局请求，并保持 shell 无 stdout 输出。"""

        def connection_made(self, connection):
            original = connection._process_keepalive_at_openssh_dot_com_global_request

            def observe_keepalive(packet):
                keepalive_received.set()
                return original(packet)

            connection._process_keepalive_at_openssh_dot_com_global_request = observe_keepalive

        def begin_auth(self, _username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "collector" and password == "password"

        def connection_lost(self, _exc):
            peer_closed.set()

    async def silent_process(process):
        """保持 shell 存活到客户端关闭，不主动写入 stdout。"""
        await process.stdin.read()

    server = await asyncssh.listen(
        "127.0.0.1", 0, server_factory=Server, process_factory=silent_process,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
    )
    try:
        connection = await _connect_ssh(
            {"username": "collector", "password": "password"}, "127.0.0.1", server.get_port(),
        )
        # 仅调整本测试已完成握手的连接，生产 _connect_ssh() 的 15 秒默认值保持不变。
        connection._client.set_keepalive(interval=.2, count_max=3)
        states = []
        collector = Collector(
            {"id": "ssh-silent", "runId": "run", "storageIdentity": "testingdevice", "initialCommands": []},
            tmp_path, connection_factory=lambda _: connection,
            on_state=lambda state, _details: states.append(state),
        )
        started = asyncio.get_running_loop().time()
        await collector.start()
        await asyncio.wait_for(keepalive_received.wait(), timeout=2)
        await asyncio.wait_for(collector.wait_closed(), timeout=15)

        assert asyncio.get_running_loop().time() - started >= 10
        assert "IDLE_TIMEOUT" in states
        await asyncio.wait_for(peer_closed.wait(), timeout=2)
        assert collector._connection_closed
    finally:
        try:
            if collector:
                await collector.stop()
            elif connection:
                await connection.close()
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_shell_keepalive_prevents_auto_logout_during_logs_and_yields_to_normal_commands(tmp_path, monkeypatch):
    """持续正文不能代替 shell 输入；正常命令存在时保活不应额外插入队列。"""
    auto_logout, keepalive_seen = asyncio.Event(), asyncio.Event()
    command_lines = bytearray()

    class Server(asyncssh.SSHServer):
        """最小真实 SSH 设备：持续输出日志，但空闲 shell 会主动登出。"""

        def begin_auth(self, _username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "collector" and password == "password"

    async def process_factory(process):
        """以独立 stdin 读协程模拟 shell 活动时间，stdout 始终输出设备日志。"""
        last_input = asyncio.get_running_loop().time()

        async def read_input():
            nonlocal last_input
            while data := await process.stdin.read(1024):
                command_lines.extend(data.encode() if isinstance(data, str) else data)
                last_input = asyncio.get_running_loop().time()
                if b":\n" in command_lines:
                    keepalive_seen.set()

        reader = asyncio.create_task(read_input())
        try:
            process.stdout.write("BusyBox built-in shell (ash)\n# ")
            await process.stdout.drain()
            while True:
                if asyncio.get_running_loop().time() - last_input > .5:
                    auto_logout.set()
                    process.stdout.write("timed out waiting for input: auto-logout\n")
                    await process.stdout.drain()
                    return
                process.stdout.write("continuous-device-log\n")
                await process.stdout.drain()
                await asyncio.sleep(.005)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    server = await asyncssh.listen(
        "127.0.0.1", 0, server_factory=Server, process_factory=process_factory,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")], line_editor=False,
    )
    collector = None
    monkeypatch.setattr(collector_module, "_SHELL_KEEPALIVE_SECONDS", .05)
    try:
        connection = await _connect_ssh(
            {"username": "collector", "password": "password"}, "127.0.0.1", server.get_port(),
        )
        collector = Collector(
            {"id": "shell-logout", "runId": "run", "storageIdentity": "testingdevice", "protocolType": "SSH",
             "initialCommands": [], "scheduledCommands": []},
            tmp_path, connection_factory=lambda _: connection,
        )
        await collector.start()
        # 连续普通命令均在 50ms 保活间隔内送达，因此保活不得插入 `:`。
        for number in range(6):
            await collector.enqueue_manual(f"normal-{number}")
            await asyncio.sleep(.025)
        assert b":\n" not in command_lines
        # 普通命令停止后，必须经现有 sender 发送 shell 内建空操作，持续日志仍不能触发登出。
        await asyncio.wait_for(keepalive_seen.wait(), timeout=.5)
        assert not auto_logout.is_set()
        assert b"normal-0\n" in command_lines and b":\n" in command_lines
        # 超过服务端 500ms 自动登出阈值仍保持会话，证明持续日志外确有 shell 输入保活。
        await asyncio.sleep(.65)
        assert not auto_logout.is_set() and not collector._closed.is_set()
    finally:
        if collector is not None:
            await collector.stop()
            assert collector._shell_keepalive is None
        server.close()
        await server.wait_closed()
