"""跨 Worker SSH 暂停恢复验证使用的回环 ISAPI 与 AsyncSSH 源。"""

import asyncio
import base64
import hmac
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import asyncssh

INITIAL_COMMANDS = ["outputClose", "outputOpen", "setDebug -m all -l 7 -d 111", "prtHardInfo"]


class DeviceInfoSource:
    """提供最小 ISAPI 认证响应，并允许测试在恢复健康检查处设置响应闸门。"""

    def __init__(self, password: str, port: int = 80) -> None:
        self.password, self.port = password, port
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.requests_seen, self.allow_requests = threading.Event(), threading.Event()
        self.allow_requests.set()

    async def start(self) -> None:
        """仅绑定回环地址；默认80用于真实 ISAPI URL，测试端口由隔离启动器适配。"""
        authorization = "Basic " + base64.b64encode(f"collector:{self.password}".encode()).decode()
        payload = (
            "<DeviceInfo><model>LOCAL-RESUME</model><subSerialNumber>"
            f"{uuid4().hex}</subSerialNumber><firmwareVersion>V1</firmwareVersion>"
            "<firmwareReleasedDate>build local</firmwareReleasedDate></DeviceInfo>"
        ).encode()
        source = self

        class Handler(BaseHTTPRequestHandler):
            """只允许认证设备信息请求，不能成为通用 HTTP 服务。"""

            def do_GET(self) -> None:
                if self.path != "/ISAPI/System/deviceInfo":
                    self.send_error(404)
                    return
                if not hmac.compare_digest(self.headers.get("Authorization", ""), authorization):
                    self.send_error(401)
                    return
                source.requests_seen.set()
                if not source.allow_requests.wait(15):
                    self.send_error(503)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args) -> None:
                """认证头不得写入测试输出。"""

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    async def close(self) -> None:
        """等待 HTTP 监听线程退出，避免临时端口泄漏到下一轮。"""
        if self.server is not None:
            await asyncio.to_thread(self.server.shutdown)
            self.server.server_close()
        if self.thread is not None:
            await asyncio.to_thread(self.thread.join)


class SshSource:
    """连续发送含会话和源序号的日志，并记录每个 shell 实际收到的命令。"""

    def __init__(self, password: str) -> None:
        self.password, self.server = password, None
        self.connected, self.closed = asyncio.Event(), asyncio.Event()
        self.commands: list[bytearray] = []
        self.sent: list[bytes] = []
        self.active = self.connection_count = 0
        self.emitting = asyncio.Event()
        self.emitting.set()

    async def start(self) -> None:
        """启动真实回环 AsyncSSH 服务；每200ms输出一次，避免空闲重连。"""
        source = self

        class Server(asyncssh.SSHServer):
            def connection_made(self, _connection) -> None:
                source.connection_count += 1
                source.active += 1
                self.number = source.connection_count
                source.closed.clear()
                source.connected.set()

            def connection_lost(self, _error) -> None:
                source.active -= 1
                if source.active == 0:
                    source.closed.set()

            def password_auth_supported(self) -> bool:
                return True

            def validate_password(self, username: str, password: str) -> bool:
                return username == "collector" and hmac.compare_digest(password, source.password)

        async def shell(process) -> None:
            number, payload, reader = source.connection_count, bytearray(), None
            source.commands.append(payload)

            async def read_stdin() -> None:
                while chunk := await process.stdin.read(4096):
                    payload.extend(chunk if isinstance(chunk, bytes) else chunk.encode())

            try:
                reader = asyncio.create_task(read_stdin())
                sequence = 0
                while not reader.done():
                    await source.emitting.wait()
                    line = f"sourceSession={number} sourceSeq={sequence:06d} payload=live\n".encode()
                    process.stdout.write(line)
                    await process.stdout.drain()
                    source.sent.append(line)
                    sequence += 1
                    await asyncio.sleep(0.2)
                await reader
            finally:
                if reader is not None and not reader.done():
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)

        self.server = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_factory=Server,
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
            process_factory=shell,
            encoding=None,
            line_editor=False,
        )
        self.port = self.server.get_port()

    async def wait_commands(self, sessions: int, timeout: float = 30) -> None:
        """确认每个 SSH 会话均按固定初始化顺序收到命令和一个定时 probe。"""
        expected = INITIAL_COMMANDS + ["probe"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            received = self.command_lines()
            if len(received) >= sessions and all(items[:5] == expected for items in received[:sessions]):
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(f"源端命令不完整: {self.command_lines()!r}")

    def command_lines(self) -> list[list[str]]:
        """按 shell 换行还原服务端收到的命令，避免把命令回显误当发送成功。"""
        return [
            [line.rstrip(b"\r").decode("utf-8", "replace") for line in bytes(payload).split(b"\n") if line]
            for payload in self.commands
        ]

    async def suspend_after(self, session: int, minimum_lines: int = 12) -> list[bytes]:
        """暂停源端日志输出并返回该会话已发送正文；调用方须在十秒内继续流程。"""
        deadline = time.monotonic() + 10
        marker = f"sourceSession={session} ".encode()
        while time.monotonic() < deadline:
            lines = [line for line in self.sent if line.startswith(marker)]
            if len(lines) >= minimum_lines:
                self.emitting.clear()
                return lines
            await asyncio.sleep(0.05)
        raise TimeoutError("未获得足够的有限源端日志样本")

    def resume_emitting(self) -> None:
        """恢复下一会话的连续源输出。"""
        self.emitting.set()

    async def close(self) -> None:
        """关闭监听器；现有连接会由 API stop 路径负责先行回收。"""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
