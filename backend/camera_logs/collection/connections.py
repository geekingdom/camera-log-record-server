"""采集网络适配器。

SSH 默认使用账号密码连接，设备更换主机密钥无需人工登记；部署可选择严格校验
known_hosts。Telnet 以二进制模式传递设备原始字节。协议保活和 TCP 保活只用于连接
存活探测，绝不向设备 shell 发送服务生成的业务命令。
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class Connection(Protocol):
    async def read(self, size: int = 65536) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


class _SshConnection:
    def __init__(self, client: Any, process: Any) -> None:
        self._client, self._process = client, process
        _tcp_keepalive(client)

    async def read(self, size: int = 65536) -> bytes:
        return await self._process.stdout.read(size)

    async def write(self, data: bytes) -> None:
        self._process.stdin.write(data)
        drain = getattr(self._process.stdin, "drain", None)
        if drain:
            await drain()

    async def close(self) -> None:
        # shell 通道收尾失败也必须关闭底层 SSH 连接，否则会占用设备有限的连接槽。
        try:
            self._process.close()
        finally:
            self._client.close()
        try:
            await asyncio.wait_for(self._client.wait_closed(), timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            abort = getattr(self._client, "abort", None)
            if abort:
                abort()
            if asyncio.current_task() and asyncio.current_task().cancelling():
                raise


class _TelnetConnection:
    def __init__(self, reader: Any, writer: Any, prefix: bytes = b"", interval: float = 30) -> None:
        self._reader, self._writer = reader, writer
        self._prefix = prefix
        _tcp_keepalive(writer)
        self._heartbeat = asyncio.create_task(self._keepalive(interval))

    async def _keepalive(self, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                # IAC NOP 是 Telnet 协议控制字节，不是 shell 输入，不会污染原始日志。
                self._writer.send_iac(b"\xff\xf1")
                await self._writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telnet 协议保活失败，关闭底层连接")
            self._writer.close()

    async def read(self, size: int = 65536) -> bytes:
        if self._prefix:
            value, self._prefix = self._prefix[:size], self._prefix[size:]
            return value
        return await self._reader.read(size)

    async def write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        self._heartbeat.cancel()
        await asyncio.gather(self._heartbeat, return_exceptions=True)
        self._writer.close()
        await self._writer.wait_closed()


async def connect(task: Mapping[str, Any]) -> Connection:
    """按任务固定端点创建连接，SSH 必须使用已确认的主机密钥。"""
    protocol = task.get("protocol") or task.get("protocolType")
    host, port = str(task["ip"]), int(task["port"])
    if protocol == "SSH":
        return await _connect_ssh(task, host, port)
    if protocol in {"TELNET_DEVICE", "TELNET_SERIAL"}:
        return await _connect_telnet(task, host, port)
    raise ValueError(f"unsupported protocol: {protocol!r}")


def _tcp_keepalive(owner: Any) -> None:
    """尽力配置 TCP 探测；应用层是否有日志仍由采集器空闲看门狗判断。"""
    getter = getattr(owner, "get_extra_info", None)
    sock = getter("socket") if getter else None
    if sock is None:
        transport = getattr(owner, "transport", None)
        sock = transport.get_extra_info("socket") if transport else None
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        elif hasattr(socket, "TCP_KEEPALIVE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 30)
    except OSError:
        return


async def _connect_ssh(task: Mapping[str, Any], host: str, port: int) -> Connection:
    """创建 SSH shell；严格指纹校验仅在部署明确启用时生效。"""
    import asyncssh

    known_hosts = None
    if task.get("verifyHostKey", False):
        configured_path = task.get("knownHosts")
        known_hosts_path = Path(str(configured_path)) if configured_path else None
        if not known_hosts_path or not known_hosts_path.is_file():
            raise ValueError("严格SSH主机指纹校验需要有效的knownHosts文件")
        known_hosts = str(known_hosts_path)
    client = None
    try:
        client = await asyncio.wait_for(asyncssh.connect(
            host, port=port, username=task["username"], password=task["password"],
            known_hosts=known_hosts, encoding=None, keepalive_interval=15, keepalive_count_max=3,
        ), timeout=30)
        process = await asyncio.wait_for(
            client.create_process(term_type=task.get("termType", "vt100"), encoding=None), timeout=30
        )
        return _SshConnection(client, process)
    except BaseException:
        # create_process 失败或取消时，连接已经由本函数取得，必须主动回收。
        if client:
            client.close()
            try:
                await asyncio.wait_for(client.wait_closed(), timeout=10)
            except BaseException:  # noqa: BLE001 - cancellation must still abort the owned transport.
                abort = getattr(client, "abort", None)
                if abort:
                    abort()
        raise


async def _connect_telnet(task: Mapping[str, Any], host: str, port: int) -> Connection:
    import telnetlib3

    reader, writer = await asyncio.wait_for(
        telnetlib3.open_connection(host=host, port=port, encoding=False), timeout=30)
    username, password = task.get("username"), task.get("password")
    try:
        prefix = b""
        if username and password:
            prefix = await _telnet_login(reader, writer, str(username), str(password), task)
        return _TelnetConnection(reader, writer, prefix=prefix, interval=float(task.get("telnetKeepaliveInterval", 30)))
    except BaseException:
        writer.close()
        await writer.wait_closed()
        raise


async def _telnet_login(reader: Any, writer: Any, username: str, password: str, task: Mapping[str, Any]) -> bytes:
    """识别登录提示并发送凭据；已读取的横幅按顺序交回采集链路，不能静默丢弃。"""
    login_prompt = str(task.get("loginPrompt", "login:")).encode()
    password_prompt = str(task.get("passwordPrompt", "Password:")).encode()
    timeout = float(task.get("loginTimeoutSeconds", 30))
    banner = await asyncio.wait_for(reader.readuntil(login_prompt), timeout)
    writer.write((username + str(task.get("newline", "\n"))).encode())
    await writer.drain()
    banner += await asyncio.wait_for(reader.readuntil(password_prompt), timeout)
    writer.write((password + str(task.get("newline", "\n"))).encode())
    await writer.drain()
    return banner
