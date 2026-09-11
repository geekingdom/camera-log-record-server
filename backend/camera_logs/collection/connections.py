"""采集网络适配器。

SSH 默认使用账号密码连接，设备更换主机密钥无需人工登记；部署可选择严格校验
known_hosts。Telnet 以二进制模式传递设备原始字节。协议保活和 TCP 保活只用于连接
存活探测，绝不向设备 shell 发送服务生成的业务命令。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import weakref
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)
CLOSE_TIMEOUT_SECONDS = 10
# 海康设备单台最多允许五个 SSH 会话。按事件循环隔离信号量，避免测试或
# 多个 Worker 进程之间错误共享 asyncio 对象；进程级限制由每个 Worker 独立执行。
MAX_SSH_CONNECTIONS_PER_ENDPOINT = 5
_ssh_limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, int], asyncio.Semaphore]] = weakref.WeakKeyDictionary()


class Connection(Protocol):
    async def read(self, size: int = 65536) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


class _SshConnection:
    def __init__(self, client: Any, process: Any, *, release_slot: Any | None = None) -> None:
        self._client, self._process = client, process
        self._release_slot = release_slot
        self._slot_released = False
        _tcp_keepalive(client)

    async def read(self, size: int = 65536) -> bytes:
        return await self._process.stdout.read(size)

    async def write(self, data: bytes) -> None:
        self._process.stdin.write(data)
        drain = getattr(self._process.stdin, "drain", None)
        if drain:
            await drain()

    async def close(self) -> None:
        """关闭 shell 与 SSH 传输；确认失败时强制中止且保留原始异常。"""
        try:
            # shell 收尾异常也不能跳过底层连接的关闭和确认。
            try:
                self._process.close()
            finally:
                self._client.close()
            await asyncio.wait_for(self._client.wait_closed(), timeout=CLOSE_TIMEOUT_SECONDS)
        except BaseException:
            _abort_transport(self._client)
            raise
        finally:
            self._release_connection_slot()

    def _release_connection_slot(self) -> None:
        """关闭成功或失败都只释放一次并唤醒等待中的建连请求。"""
        if self._slot_released:
            return
        self._slot_released = True
        if self._release_slot is not None:
            self._release_slot()


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
        try:
            await asyncio.gather(self._heartbeat, return_exceptions=True)
        finally:
            # 取消等待保活协程时仍必须关闭底层传输，不能让停止流程悬挂。
            await _close_telnet_writer(self._writer)


async def connect(task: Mapping[str, Any]) -> Connection:
    """按任务固定端点创建连接；SSH 默认免登记，可选严格主机密钥校验。"""
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


def _abort_transport(owner: Any) -> None:
    """尽力中止未能确认关闭的传输；中止失败不能覆盖原始关闭异常。"""
    for candidate in (owner, getattr(owner, "transport", None)):
        abort = getattr(candidate, "abort", None) if candidate is not None else None
        if abort is None:
            continue
        try:
            abort()
        except Exception:
            logger.exception("强制中止连接失败")
        return


async def _close_telnet_writer(writer: Any) -> None:
    """有界等待 Telnet 关闭确认，超时、取消或异常时强制中止传输。"""
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=CLOSE_TIMEOUT_SECONDS)
    except BaseException:
        _abort_transport(writer)
        raise


async def _connect_ssh(task: Mapping[str, Any], host: str, port: int) -> Connection:
    """创建 SSH shell；严格指纹校验仅在部署明确启用时生效。"""
    import asyncssh

    slot = _ssh_slot(host, port)
    # 等待名额也设置上限，避免设备长期满载时创建无界挂起任务。
    await asyncio.wait_for(slot.acquire(), timeout=30)
    slot_acquired = True

    client = None
    try:
        known_hosts = None
        if task.get("verifyHostKey", False):
            configured_path = task.get("knownHosts")
            known_hosts_path = Path(str(configured_path)) if configured_path else None
            if not known_hosts_path or not known_hosts_path.is_file():
                raise ValueError("严格SSH主机指纹校验需要有效的knownHosts文件")
            known_hosts = str(known_hosts_path)
        client = await asyncio.wait_for(asyncssh.connect(
            host, port=port, username=task["username"], password=task["password"],
            known_hosts=known_hosts, encoding=None, keepalive_interval=15, keepalive_count_max=3,
        ), timeout=30)
        process = await asyncio.wait_for(
            client.create_process(term_type=task.get("termType", "vt100"), encoding=None), timeout=30
        )
        connection = _SshConnection(client, process, release_slot=slot.release)
        # 成功后关闭路径接管名额，异常路径的 finally 不得提前释放。
        slot_acquired = False
        return connection
    except BaseException as error:
        # create_process 失败或取消时，连接已经由本函数取得，必须主动回收。
        if client:
            if isinstance(error, asyncio.CancelledError):
                # 已取消的建连协程不能可靠地等待 wait_closed；把收尾交给独立任务，
                # 名额在其完成回调才归还，避免第六路抢在旧 SSH 释放前建立。
                cleanup = asyncio.create_task(_cleanup_failed_ssh_client(client))
                cleanup.add_done_callback(lambda _future: slot.release())
                slot_acquired = False
            else:
                await _cleanup_failed_ssh_client(client)
        raise
    finally:
        # 必须在 client 清理完成后才归还名额。否则第六路可能在旧底层 SSH
        # 仍未释放时建立，瞬时超过设备允许的五连接限制。
        if slot_acquired:
            slot.release()


def _ssh_slot(host: str, port: int) -> asyncio.Semaphore:
    """返回当前 Worker 对指定 SSH 端点的五连接租约池。"""
    loop = asyncio.get_running_loop()
    pools = _ssh_limiters.setdefault(loop, {})
    return pools.setdefault((host, port), asyncio.Semaphore(MAX_SSH_CONNECTIONS_PER_ENDPOINT))


async def _cleanup_failed_ssh_client(client: Any) -> None:
    """Shell 建立失败后有界回收底层 SSH；取消路径也必须留下可执行的收尾。"""
    try:
        client.close()
        await asyncio.wait_for(client.wait_closed(), timeout=CLOSE_TIMEOUT_SECONDS)
    except BaseException:
        # close 本身也可能失败；此时强制中止且不覆盖原始建连失败。
        logger.exception("SSH shell 创建失败后的连接收尾异常")
        _abort_transport(client)


def _log_telnet_client(**kwargs):
    """通过库提供的工厂接入日志接收优化，保留连接生命周期和协商机制。"""
    from camera_logs.collection.telnet_client import LogTelnetClient

    return LogTelnetClient(**kwargs)


async def _connect_telnet(task: Mapping[str, Any], host: str, port: int) -> Connection:
    import telnetlib3

    reader, writer = await asyncio.wait_for(
        telnetlib3.open_connection(host=host, port=port, encoding=False,
                                 client_factory=_log_telnet_client), timeout=30)
    username, password = task.get("username"), task.get("password")
    try:
        prefix = b""
        if username and password:
            prefix = await _telnet_login(reader, writer, str(username), str(password), task)
        return _TelnetConnection(reader, writer, prefix=prefix, interval=float(task.get("telnetKeepaliveInterval", 30)))
    except BaseException:
        # 登录失败的根因必须返回给调用方；清理失败只记录并强制中止传输。
        try:
            await _close_telnet_writer(writer)
        except BaseException:
            logger.exception("Telnet 登录失败后的连接收尾异常")
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
