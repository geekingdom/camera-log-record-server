"""使用本地 AsyncSSH 服务验证协议保活不延长采集空闲超时。"""

import asyncio

import asyncssh
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
