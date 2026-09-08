"""服务压测的本地 Telnet 负载源与流式下载归档校验工具，不访问真实设备。"""

import asyncio
import hashlib
import re
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import telnetlib3

MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_PENDING_BYTES = 65536 + 22
PREFIX = re.compile(rb"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] ")
BODY = re.compile(rb"^route=(\d+) seq=(\d+) ")
PART = re.compile(r"(?:^|-)part-(\d+)\.log$")


@dataclass
class LoadSource:
    """本地 Telnet 模拟源；主编排在确认连接后设置 release 并主动调用 emit。"""
    route: int
    bind_host: str
    lines_per_second: int
    line_bytes: int
    port: int = 0
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    peer_closed: asyncio.Event = field(default_factory=asyncio.Event)
    source_lines: int = 0
    source_bytes: int = 0
    source_sha256: str = ""
    elapsed_seconds: float = 0
    max_tick_lag_seconds: float = 0
    connection_count: int = 0
    failure: BaseException | None = None
    _server: Any = field(default=None, init=False, repr=False)
    _writer: Any = field(default=None, init=False, repr=False)
    _handlers: set[asyncio.Task] = field(default_factory=set, init=False, repr=False)
    _emitter: asyncio.Task | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)

    def line(self, sequence: int) -> bytes:
        """按路由和连续序号生成定长 ASCII 正文，填充可由路由和序号确定性复现。"""
        prefix = f"route={self.route:04d} seq={sequence:09d} ".encode("ascii")
        if self.line_bytes < len(prefix) + 1:
            raise ValueError("line_bytes 小于路由和序号正文所需长度")
        remaining = self.line_bytes - len(prefix) - 1
        token = f"{self.route:04x}{sequence:09x}".encode("ascii")
        fill = token * ((remaining + len(token) - 1) // len(token))
        return prefix + fill[:remaining] + b"\n"

    async def start(self) -> None:
        """绑定随机本地 Telnet 端口，开始等待服务端建立采集连接。"""
        self._server = await telnetlib3.create_server(
            host=self.bind_host, port=0, shell=self._serve, encoding=False, connect_maxwait=.05, timeout=False,
        )
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def _serve(self, reader: Any, writer: Any) -> None:
        """记录连接并仅等待对端关闭，日志发送由 emit 集中控制以免重连重复正文。"""
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        self.connection_count += 1
        if self.connection_count > 1:
            self.failure = RuntimeError("压测源发生重连，禁止重复发送正文")
        self._writer = writer
        self.connected.set()
        try:
            while await reader.read(65536):
                pass
        except asyncio.CancelledError:
            raise
        except OSError as error:
            self.failure = error
        finally:
            writer.close()
            await writer.wait_closed()
            self.peer_closed.set()
            if task is not None:
                self._handlers.discard(task)

    async def emit(self, seconds: int) -> None:
        """等待 release 后每 100ms 写入精确行数；完成后保持连接直到 API 主动停止。"""
        if seconds < 0 or self._started:
            raise ValueError("emit 只能执行一次且 seconds 不能为负数")
        self._started = True
        self._emitter = asyncio.current_task()
        digest = hashlib.sha256()
        try:
            await self.release.wait()
            await self.connected.wait()
            if self.failure is not None or self.connection_count != 1 or self._writer is None:
                raise RuntimeError("Telnet 连接在发送前不可用")
            started, deadline = time.monotonic(), time.monotonic()
            for tick in range(seconds * 10):
                count = ((tick + 1) * self.lines_per_second // 10) - (tick * self.lines_per_second // 10)
                payload = b"".join(self.line(self.source_lines + offset) for offset in range(count))
                self._writer.write(payload)
                await self._writer.drain()
                digest.update(payload)
                self.source_lines += count
                self.source_bytes += len(payload)
                deadline += .1
                self.max_tick_lag_seconds = max(self.max_tick_lag_seconds, time.monotonic() - deadline)
                await asyncio.sleep(max(0, deadline - time.monotonic()))
            self.elapsed_seconds = time.monotonic() - started
            self.source_sha256 = digest.hexdigest()
            self.finished.set()
        except BaseException as error:
            if not isinstance(error, asyncio.CancelledError):
                self.failure = error
            raise
        finally:
            self._emitter = None

    async def close(self) -> None:
        """取消未完成发送和连接处理，并关闭监听端口，异常路径也保证资源回收。"""
        self.release.set()
        emitter = self._emitter
        if emitter is not None and emitter is not asyncio.current_task():
            emitter.cancel()
        if self._writer is not None:
            self._writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in list(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        if emitter is not None and emitter is not asyncio.current_task():
            await asyncio.gather(emitter, return_exceptions=True)


def _order(value: str) -> list[object]:
    """按小时名称中的数字自然排序，避免字符串排序颠倒分卷或小时。"""
    return [int(item) if item.isdigit() else item for item in re.split(r"(\d+)", value)]


def _line(state: dict[str, Any], raw: bytes) -> None:
    """校验一条已完整的服务器前缀日志，并累计全局及单路摘要和连续序号。"""
    match = PREFIX.match(raw)
    if match is None:
        raise AssertionError("日志缺少服务器上海时间前缀")
    try:
        datetime.strptime(match.group(1).decode("ascii"), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
    except ValueError as error:
        raise AssertionError("日志时间前缀格式错误") from error
    body = raw[match.end():] + b"\n"
    route = BODY.match(body)
    if route is None:
        raise AssertionError("日志正文缺少路由或连续序号")
    route_id, sequence = int(route.group(1)), int(route.group(2))
    expected = state["next"].get(route_id, 0)
    if sequence != expected:
        raise AssertionError(f"路由 {route_id} 序号不连续，期望 {expected}，实际 {sequence}")
    state["next"][route_id] = expected + 1
    state["routes"].setdefault(route_id, hashlib.sha256()).update(body)
    state["digest"].update(body)
    state["lines"] += 1


def _tar(stream: Any, label: str, state: dict[str, Any]) -> None:
    """顺序读取一个 tar.gz，拒绝非日志、重复成员、超限成员和缺失的 part 序号。"""
    previous = 0
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for member in archive:
            part = PART.search(member.name)
            if not member.isfile() or not member.name.endswith(".log") or part is None:
                raise AssertionError("下载包包含非日志成员")
            member_id = (label, member.name)
            if member.size > MAX_MEMBER_BYTES or member_id in state["members"]:
                raise AssertionError("下载包包含重复或超限日志成员")
            number = int(part.group(1))
            if number != previous + 1:
                raise AssertionError(f"{label} 的 part 序号不连续")
            previous = number
            state["members"].add(member_id)
            source = archive.extractfile(member)
            if source is None:
                raise AssertionError("下载日志成员无法读取")
            state["logParts"] += 1
            while chunk := source.read(64 * 1024):
                state["storedBytes"] += len(chunk)
                complete = (state["pending"] + chunk).split(b"\n")
                state["pending"] = complete.pop()
                if len(state["pending"]) > MAX_PENDING_BYTES:
                    raise AssertionError("下载日志存在超过单行上限的无换行内容")
                for raw in complete:
                    _line(state, raw)


def _download(path: Path, state: dict[str, Any]) -> None:
    """校验一个下载文件；ZIP 内小时包排序，外部多文件顺序由调用方的 UTC 列表决定。"""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as bundle:
            entries = sorted(bundle.infolist(), key=lambda item: _order(item.filename))
            if not entries or any(item.is_dir() or not item.filename.endswith(".tar.gz") or item.compress_type != zipfile.ZIP_STORED for item in entries):
                raise AssertionError("ZIP 下载必须只包含 STORE 的小时 tar.gz")
            if len({item.filename for item in entries}) != len(entries):
                raise AssertionError("ZIP 下载包含重复小时包")
            for entry in entries:
                with bundle.open(entry) as stream:
                    _tar(stream, entry.filename, state)
    else:
        with path.open("rb") as stream:
            _tar(stream, path.name, state)


def verify_download(path: Path | list[Path], expected_sha256: str, expected_lines: int) -> dict:
    """流式校验单个或按 UTC 小时升序的多个下载，统一重组跨分卷和跨小时半行。"""
    paths = [Path(item) for item in path] if isinstance(path, list) else [Path(path)]
    if not paths:
        raise ValueError("至少需要一个下载文件")
    state = {"digest": hashlib.sha256(), "routes": {}, "next": {}, "lines": 0, "members": set(),
             "pending": b"", "storedBytes": 0, "logParts": 0}
    for item in paths:
        _download(item, state)
    if state["pending"]:
        raise AssertionError("下载日志末尾存在截断半行")
    actual = state["digest"].hexdigest()
    if actual != expected_sha256 or state["lines"] != expected_lines:
        raise AssertionError("下载正文摘要或行数与压测源不一致")
    return {"sha256": actual, "lines": state["lines"], "storedBytes": state["storedBytes"],
            "logParts": state["logParts"],
            "routeSha256": {route: digest.hexdigest() for route, digest in state["routes"].items()},
            "routeLines": state["next"]}
