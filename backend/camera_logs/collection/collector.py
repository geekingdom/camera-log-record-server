"""单路采集器：单连接接收、FIFO 原始字节写入和串行命令发送。

本模块不按日志正文去重，也不在原始文件插入服务生成文本；每个实例只处理
一个 task/run/session。连接断开、空闲超时和人工停止均由运行时决定是否重连。
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from camera_logs.logs.storage import HourlyWriter

from .line_prefix import LinePrefixer


class AsyncConnection(Protocol):
    async def read(self, size: int = 65536) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LogChunk:
    task_id: str
    run_id: str
    session_id: str
    sequence: int
    data: bytes
    offset: int
    path: str


Callback = Callable[..., Awaitable[Any] | Any]


class BudgetExhausted(RuntimeError):
    """定时命令在写入 socket 前被持久化执行预算拒绝。"""


async def _call(callback: Callback | None, *args: Any) -> Any:
    if callback is None:
        return None
    value = callback(*args)
    return await value if inspect.isawaitable(value) else value


class Collector:
    def __init__(
        self,
        task: Mapping[str, Any],
        root: Path,
        *,
        connection_factory: Callback,
        on_log: Callback | None = None,
        on_state: Callback | None = None,
        on_archive: Callback | None = None,
        reserve_execution: Callback | None = None,
        update_execution: Callback | None = None,
    ) -> None:
        self.task = dict(task)
        self.task_id = str(task.get("id") or task.get("_id"))
        self.run_id = str(task.get("runId") or uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self._root, self._factory = Path(root), connection_factory
        self._on_log, self._on_state, self._on_archive = on_log, on_state, on_archive
        self._reserve, self._update = reserve_execution, update_execution
        self._connection: AsyncConnection | None = None
        self._connection_closed = False
        self._writer = HourlyWriter(
            self.task_id,
            self.run_id,
            self.session_id,
            self._root,
            task_name=str(self.task.get("name") or self.task_id),
            device_ip=str(self.task.get("ip") or "unknown"),
        )
        self._queue: asyncio.PriorityQueue[
            tuple[int, int, str, str, str | None, float, Callback | None, asyncio.Future[None]]
        ] = (
            asyncio.PriorityQueue()
        )
        self._sender: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._scheduled: list[asyncio.Task[None]] = []
        self._closed = asyncio.Event()
        self._accepting_commands = True
        self._counter = 0
        self._command_status: dict[str, str] = {}
        self._prompt_waiter: tuple[bytes, asyncio.Future[None], int] | None = None
        self._sending_future: asyncio.Future[None] | None = None
        self._received_epoch = 0
        self._prefixer = LinePrefixer()
        self._terminal_error: Exception | None = None

    async def start(self) -> None:
        if self._connection is not None:
            return
        await _call(
            self._on_state, "CONNECTING", {"taskId": self.task_id, "sessionId": self.session_id}
        )
        connection = await _call(self._factory, self.task)
        if connection is None:
            raise ConnectionError("connection factory did not return a connection")
        self._connection = connection
        self._sender = asyncio.create_task(self._sender_loop())
        # 必须先启动接收协程：初始化命令发送后设备可能立即返回提示符。
        self._reader = asyncio.create_task(self._reader_loop())
        try:
            for item in self.task.get("initialCommands", []):
                await self._send_initial(item)
            for position, item in enumerate(self.task.get("scheduledCommands", [])):
                self._scheduled.append(asyncio.create_task(self._scheduled_loop(position, item)))
        except BaseException:
            # 初始化失败时本实例已经拥有连接和后台协程，必须立即进入同一关闭路径。
            await self.stop()
            raise
        await _call(
            self._on_state, "COLLECTING", {"taskId": self.task_id, "sessionId": self.session_id}
        )

    async def _send_initial(self, item: Mapping[str, Any]) -> None:
        command = str(item["command"])
        await self._enqueue(
            command,
            str(item.get("newline", "\n")),
            priority=0,
            prompt=item.get("prompt"),
            timeout_seconds=float(item.get("timeoutSeconds", 30)),
        )
        delay = float(item.get("delaySeconds", 0))
        if delay > 0:
            await asyncio.sleep(delay)

    async def enqueue_manual(
        self,
        command: str,
        *,
        newline: str = "\n",
        prompt: str | None = None,
        timeout_seconds: float = 30,
        delay_seconds: float = 0,
    ) -> str:
        """将人工命令加入唯一发送队列；提示符和发送后延时都在本会话内串行完成。"""
        if not command or not command.strip() or not self._accepting_commands or self._connection is None:
            raise RuntimeError("collector is not accepting manual commands")
        command_id = str(uuid.uuid4())
        self._command_status[command_id] = "QUEUED"
        await self._enqueue(
            command,
            newline,
            priority=1,
            command_id=command_id,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
        )
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        return command_id

    def command_status(self, command_id: str) -> str | None:
        return self._command_status.get(command_id)

    async def wait_for_command(self, command_id: str) -> None:
        while self._command_status.get(command_id) == "QUEUED":
            await asyncio.sleep(0)

    async def _enqueue(
        self,
        command: str,
        newline: str,
        *,
        priority: int,
        command_id: str | None = None,
        prompt: str | None = None,
        timeout_seconds: float = 30,
        before_send: Callback | None = None,
    ) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._counter += 1
        await self._queue.put((priority, self._counter, command, newline, prompt, timeout_seconds, before_send, future))
        try:
            await future
        except Exception:
            if command_id:
                self._command_status[command_id] = "UNKNOWN"
            raise
        else:
            if command_id:
                self._command_status[command_id] = "SENT"

    async def _sender_loop(self) -> None:
        while True:
            _, _, command, newline, prompt, timeout_seconds, before_send, future = await self._queue.get()
            self._sending_future = future
            try:
                if future.cancelled():
                    continue
                if self._connection is None or self._connection_closed or not self._accepting_commands:
                    raise ConnectionError("connection closed")
                # 预算在真正写 socket 前才占用，取消排队命令不会消耗次数。
                if before_send and not await _call(before_send):
                    raise BudgetExhausted("scheduled command budget is exhausted")
                if prompt:
                    prompt_future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                    # Install before write so an immediate device response cannot be missed.
                    self._prompt_waiter = (prompt.encode(), prompt_future, self._received_epoch)
                await self._connection.write((command + newline).encode())
                if prompt:
                    try:
                        await asyncio.wait_for(prompt_future, timeout_seconds)
                    finally:
                        self._prompt_waiter = None
                if future and not future.done():
                    future.set_result(None)
            except Exception as error:  # noqa: BLE001 - 每个发送失败都必须结束对应等待 future。
                if future and not future.done():
                    future.set_exception(error)
            finally:
                self._sending_future = None
                self._queue.task_done()

    async def _scheduled_loop(self, position: int, item: Mapping[str, Any]) -> None:
        total, interval = int(item["totalExecutions"]), float(item["intervalSeconds"])
        command_id = str(item.get("id") or f"scheduled-{position}")
        for execution in range(total):
            await asyncio.sleep(interval)
            if self._closed.is_set():
                return
            detail = {"taskId": self.task_id, "runId": self.run_id, "execution": execution + 1}
            try:
                before_send = (
                    (lambda command_id=command_id, detail=detail: _call(self._reserve, command_id, detail))
                    if self._reserve is not None else None
                )
                await self._enqueue(
                    str(item["command"]),
                    str(item.get("newline", "\n")),
                    priority=2,
                    prompt=item.get("prompt"),
                    timeout_seconds=float(item.get("timeoutSeconds", 30)),
                    before_send=before_send,
                )
                await _call(self._update, command_id, "SENT", {"execution": execution + 1})
            except BudgetExhausted:
                return
            except (ConnectionError, OSError, TimeoutError) as error:
                await _call(
                    self._update,
                    command_id,
                    "UNKNOWN",
                    {"execution": execution + 1, "error": str(error)},
                )
                return

    async def _reader_loop(self) -> None:
        pending: list[tuple[bytes, object]] = []
        prompt_tail = b""
        flush_deadline = asyncio.get_running_loop().time() + 0.1
        last_received = asyncio.get_running_loop().time()
        idle_timeout = float(self.task.get("logIdleTimeoutSeconds", 10))
        try:
            assert self._connection is not None
            while self._accepting_commands:
                try:
                    data = await asyncio.wait_for(self._connection.read(), timeout=0.1)
                except TimeoutError:
                    if pending:
                        to_flush, pending = pending, []
                        await self._flush(to_flush)
                    await self._publish_archives()
                    await self._writer.sync_due()
                    flush_deadline = asyncio.get_running_loop().time() + 0.1
                    # 只有设备实际输出重置此计时，协议心跳和服务端动作不算日志。
                    if asyncio.get_running_loop().time() - last_received >= idle_timeout:
                        await _call(self._on_state, "IDLE_TIMEOUT", {"taskId": self.task_id, "sessionId": self.session_id})
                        break
                    continue
                except (ConnectionError, OSError) as error:
                    # 网络读失败是可恢复会话故障；不把它升级为终止任务错误。
                    await _call(
                        self._on_state,
                        "READ_ERROR",
                        {"taskId": self.task_id, "sessionId": self.session_id, "error": str(error)},
                    )
                    break
                if not data:
                    break
                last_received = asyncio.get_running_loop().time()
                self._received_epoch += 1
                if self._prompt_waiter:
                    needle, waiter, after_epoch = self._prompt_waiter
                    if self._received_epoch > after_epoch and needle in prompt_tail + data and not waiter.done():
                        waiter.set_result(None)
                    prompt_tail = (prompt_tail + data)[-max(len(needle) - 1, 0):]
                received_at = datetime.now(UTC)
                pending.append((self._prefixer.prefix(data, received_at), received_at))
                if sum(len(chunk) for chunk, _ in pending) >= 256 * 1024 or asyncio.get_running_loop().time() >= flush_deadline:
                    to_flush, pending = pending, []
                    await self._flush(to_flush)
                    flush_deadline = asyncio.get_running_loop().time() + 0.1
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 将读取异常转为可审计状态，再由运行时重连。
            self._terminal_error = error
            await _call(
                self._on_state,
                "READ_ERROR",
                {"taskId": self.task_id, "sessionId": self.session_id, "error": str(error)},
            )
        finally:
            cleanup_error: Exception | None = None
            try:
                await self._close_connection()
                if pending:
                    to_flush, pending = pending, []
                    await self._flush(to_flush)
                archive = await self._writer.close()
                if archive:
                    await _call(self._on_archive, archive)
                await self._publish_archives()
            except Exception as error:  # noqa: BLE001 - 收尾失败仍必须唤醒运行时重连。
                cleanup_error = error
                await _call(
                    self._on_state,
                    "READ_ERROR",
                    {"taskId": self.task_id, "sessionId": self.session_id, "error": str(error)},
                )
            finally:
                for task in self._scheduled:
                    task.cancel()
                if self._sender and self._sender is not asyncio.current_task():
                    self._fail_queued(ConnectionError("connection closed before command dispatch"))
                    self._sender.cancel()
                self._closed.set()
                await _call(
                    self._on_state, "CLOSED", {"taskId": self.task_id, "sessionId": self.session_id}
                )
            if cleanup_error:
                self._terminal_error = cleanup_error
                raise cleanup_error

    async def _flush(self, pending: list[tuple[bytes, object]]) -> None:
        positions = await self._writer.write_many(pending)
        for (data, _), position in zip(pending, positions, strict=True):
            await _call(
                self._on_log,
                LogChunk(
                    self.task_id,
                    self.run_id,
                    self.session_id,
                    position.sequence,
                    data,
                    position.offset,
                    str(position.path),
                ),
            )
        await self._publish_archives()

    async def _publish_archives(self) -> None:
        for archive in self._writer.drain_archives():
            await _call(self._on_archive, archive)
        for error in self._writer.drain_archive_errors():
            await _call(
                self._on_state,
                "ARCHIVE_ERROR",
                {"taskId": self.task_id, "sessionId": self.session_id, "error": str(error)},
            )

    async def stop(self) -> None:
        self._accepting_commands = False
        for task in self._scheduled:
            task.cancel()
        await self._close_connection()
        if self._reader:
            # 先断开网络使 read 返回，再等待收尾落盘；禁止取消最终 flush/归档路径。
            await self._reader
        if self._sender:
            self._fail_queued(ConnectionError("collector stopped before command dispatch"))
            self._sender.cancel()
            try:
                await self._sender
            except asyncio.CancelledError:
                pass
        self._closed.set()
        if self._terminal_error:
            raise self._terminal_error

    def _fail_queued(self, error: Exception) -> None:
        if self._sending_future and not self._sending_future.done():
            self._sending_future.set_exception(error)
        while True:
            try:
                *_, future = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not future.done():
                future.set_exception(error)
            self._queue.task_done()

    async def _close_connection(self) -> None:
        if self._connection is None or self._connection_closed:
            return
        self._connection_closed = True
        await self._connection.close()

    async def wait_closed(self) -> None:
        await self._closed.wait()
