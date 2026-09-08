"""压测专用 SSH 与 ISAPI 模拟设备，临时密钥和密码只保留在内存中。"""

import asyncio
import base64
import hmac
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import asyncssh
from service_benchmark_io import LoadSource


class SshLoadSource(LoadSource):
    """复用逐路发送与摘要规则，仅替换成经过密码认证的真实 SSH 传输。"""

    def __init__(self, *args, password: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.password = password
        self.connections = set()
        self.transport_closed = asyncio.Event()

    async def start(self):
        source = self

        class Server(asyncssh.SSHServer):
            """记录包括认证失败在内的所有底层连接，供收尾完整回收。"""
            def connection_made(self, connection):
                self.connection = connection
                source.connections.add(connection)
                source.transport_closed.clear()

            def connection_lost(self, _error):
                source.connections.discard(self.connection)
                if not source.connections:
                    source.transport_closed.set()

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                return username == "benchmark" and hmac.compare_digest(password, source.password)

        async def shell(process):
            await source._serve(process.stdin, process.stdout)

        self._server = await asyncssh.listen(self.bind_host, 0, server_factory=Server,
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
            process_factory=shell, encoding=None, line_editor=False)
        self.port = self._server.get_port()

    async def close(self):
        """退出时关闭所有加密传输，不能仅关闭 shell 通道或监听 socket。"""
        try:
            await super().close()
        finally:
            connections = list(self.connections)
            for connection in connections:
                connection.close()
            await asyncio.gather(*(connection.wait_closed() for connection in connections))


class DeviceInfoSource:
    """让合成 SSH 任务也经过正式网络资源认证；80 端口被占用时直接报错。"""

    def __init__(self, host: str, password: str, port: int = 80):
        self.host, self.password, self.port = host, password, port
        self.server = self.thread = None

    async def start(self):
        authorization = "Basic " + base64.b64encode(f"benchmark:{self.password}".encode()).decode()
        serial = uuid4().hex
        payload = (f'<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                   f'<model>SSH-BENCHMARK</model><subSerialNumber>{serial}</subSerialNumber>'
                   '<firmwareVersion>V1.0</firmwareVersion><firmwareReleasedDate>build synthetic</firmwareReleasedDate>'
                   '</DeviceInfo>').encode()

        class Handler(BaseHTTPRequestHandler):
            """只提供只读设备信息，避免模拟器接受其他设备命令。"""
            def do_GET(self):
                if self.path != "/ISAPI/System/deviceInfo":
                    self.send_error(404)
                    return
                if not hmac.compare_digest(self.headers.get("Authorization", ""), authorization):
                    self.send_error(401)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                """模拟器不输出请求头和认证信息。"""

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    async def close(self):
        """停止接收请求并等待监听线程退出，异常启动时也允许调用。"""
        if self.server:
            await asyncio.to_thread(self.server.shutdown)
            self.server.server_close()
        if self.thread:
            await asyncio.to_thread(self.thread.join)
