"""单路采集器：单连接接收、FIFO 原始字节写入和串行命令发送。

本模块保留重复正文，仅按约定添加上海时区行首时间戳，不混入命令审计文本；每个
实例只处理一个 task/run/session。连接断开、空闲超时和人工停止均由运行时决定是否重连。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncssh

from camera_logs.common.write_metrics import WriteLatency
from camera_logs.logs.storage import HourlyWriter

from .command_dispatcher import BudgetExhausted, CommandChannelBlocked, CommandDispatcher
from .contracts import AsyncConnection, Callback, LogChunk
from .contracts import call_callback as _call
from .line_prefix import LinePrefixer
from .psh_dialogue import PshSwitchError
from .scheduled_supervision import supervise_scheduled


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
        resolve_debug_password: Callback | None = None,
        on_debug: Callback | None = None,
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
        self._connection_close_lock = asyncio.Lock()
        self._writer = HourlyWriter(
            self.task_id,
            self.run_id,
            self.session_id,
            self._root,
            task_name=str(self.task.get("name") or self.task_id),
            device_ip=str(self.task.get("ip") or "unknown"),
            storage_identity=self.task["storageIdentity"],
        )
        self.write_latency = WriteLatency()
        self._sender: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._scheduled: list[asyncio.Task[None]] = []
        self._coredump_monitor: asyncio.Task[None] | None = None
        self._coredump_cleanup: Any | None = None
        self._coredump_report: Callback | None = None
        self._closed = asyncio.Event()
        self._accepting_commands = True
        self._stopping_commands = False
        self._initializing = True
        self._command_status: dict[str, str] = {}
        self._prefixer = LinePrefixer()
        self._terminal_error: Exception | None = None
        self._commands = CommandDispatcher(
            self.task | {"id": self.task_id, "runId": self.run_id, "sessionId": self.session_id},
            resolve_debug_password=resolve_debug_password,
            on_debug=on_debug,
            connection=lambda: self._connection,
            accepting=lambda: self._accepting_commands,
            stopping=lambda: self._stopping_commands,
            closed=lambda: self._connection_closed,
        )

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
            if any(str(item["command"]).strip() == "debug" for item in self.task.get("initialCommands", [])):
                await self._commands.observe_initial_mode()
            for item in self.task.get("initialCommands", []):
                await self._send_initial(item)
            self._initializing = False
            # 定时预算要求当前状态已持久化；发布完成后再开始首次间隔计时。
            await _call(
                self._on_state, "COLLECTING", {"taskId": self.task_id, "sessionId": self.session_id}
            )
            for position, item in enumerate(self.task.get("scheduledCommands", [])):
                self._scheduled.append(asyncio.create_task(supervise_scheduled(self, position, item)))
        except BaseException:
            # 初始化失败时本实例已经拥有连接和后台协程，必须立即进入同一关闭路径。
            await self.stop()
            raise

    async def _send_initial(self, item: Mapping[str, Any]) -> None:
        command = str(item["command"])
        try:
            await self._enqueue(
                command,
                str(item.get("newline", "\n")),
                priority=0,
                prompt=item.get("prompt"),
                timeout_seconds=float(item.get("timeoutSeconds", 30)),
            )
        except (CommandChannelBlocked, PshSwitchError):
            # 调试失败不应关闭采集；恢复未确认时跳过本次初始化项，避免被解释成口令。
            return
        delay = float(item.get("delaySeconds", 0))
        if delay > 0:
            await asyncio.sleep(delay)

    def start_coredump_monitor(
        self, server: str, root: str, report: Callback, guard: Callback | None = None,
        cleanup_guard: Callback | None = None, resolve_target: Callback | None = None,
        wait_after_false: bool = False,
    ) -> None:
        """挂载监控与定时协程共用会话取消/回收机制，不引入额外连接。"""
        from .coredump_monitor import start_monitor

        start_monitor(self, server, root, report, guard=guard, cleanup_guard=cleanup_guard,
                      resolve_target=resolve_target, wait_after_false=wait_after_false)

    async def enqueue_manual(
        self,
        command: str,
        *,
        newline: str = "\n",
        prompt: str | None = None,
        timeout_seconds: float = 30,
        delay_seconds: float = 0,
        session_guard: Callback | None = None,
    ) -> str:
        """将人工命令加入唯一发送队列；提示符和发送后延时都在本会话内串行完成。"""
        if (
            not command or not command.strip() or self._initializing or not self._accepting_commands
            or self._stopping_commands or self._connection is None
        ):
            raise RuntimeError("collector is not accepting manual commands")
        # 普通命令也进入唯一发送队列，由 sender 在需要时重新确认 shell；
        # 首次 debug 恢复失败不能在入队处永久锁住后续命令。
        command_id = str(uuid.uuid4())
        self._command_status[command_id] = "QUEUED"
        await self._enqueue(
            command,
            newline,
            priority=1,
            command_id=command_id,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            session_guard=session_guard,
        )
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        return command_id

    async def ensure_ash_for_monitor(self, *, session_guard: Callback | None = None) -> None:
        """资源监控复用 PSH 切换入口；未知模式先由 ls 探测，正常 ASH 不写 debug。"""
        await self._commands.ensure_ash(timeout_seconds=10, session_guard=session_guard)

    async def capture_monitor_command(
        self, command: str, *, session_guard: Callback | None = None, timeout_seconds: float = 10,
    ) -> bytes:
        """在现有会话中捕获一条低优先级监控命令的有限输出。"""
        if self._initializing or self._closed.is_set() or not self._accepting_commands:
            raise ConnectionError("collector is not accepting monitor commands")
        return await self._commands.capture_command(
            command, timeout_seconds=timeout_seconds, session_guard=session_guard,
        )

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
        session_guard: Callback | None = None,
        allow_during_shutdown: bool = False,
    ) -> None:
        try:
            await self._commands.enqueue(
                command, newline, priority=priority, prompt=prompt, timeout_seconds=timeout_seconds,
                before_send=before_send, session_guard=session_guard,
                allow_during_shutdown=allow_during_shutdown,
            )
        except Exception:
            if command_id:
                self._command_status[command_id] = "UNKNOWN"
            raise
        else:
            if command_id:
                self._command_status[command_id] = "SENT"

    async def _sender_loop(self) -> None:
        """兼容已有监督和验证入口；实际队列状态由命令派发器独占。"""
        await self._commands.sender_loop()

    async def _write_debug(self, data: bytes) -> None:
        """兼容串口调试验证入口；实际逐片写入归命令派发器管理。"""
        await self._commands.write_debug(data)

    async def _scheduled_loop(self, position: int, item: Mapping[str, Any]) -> None:
        total, interval = int(item["totalExecutions"]), float(item["intervalSeconds"])
        command_id = str(item.get("id") or f"scheduled-{position}")
        execution = 0
        while execution < total:
            await asyncio.sleep(interval)
            if self._closed.is_set():
                return
            detail = {"taskId": self.task_id, "runId": self.run_id, "sessionId": self.session_id,
                      "execution": execution + 1}
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
                execution += 1
                await _call(self._update, command_id, "SENT", detail)
            except BudgetExhausted:
                return
            except CommandChannelBlocked:
                # 未进入业务发送阶段，不消耗次数；等待完整间隔后重新确认通道。
                continue
            except PshSwitchError as error:
                execution += 1
                await _call(
                    self._update,
                    command_id,
                    "FAILED",
                    detail | {"error": str(error)},
                )
                # debug 失败只影响当前执行；下一次定时 debug 可先安全恢复后重新握手。
                continue
            except (ConnectionError, OSError, TimeoutError) as error:
                await _call(
                    self._update,
                    command_id,
                    "UNKNOWN",
                    detail | {"error": str(error)},
                )
                return

    async def _reader_loop(self) -> None:
        pending: list[tuple[bytes, object]] = []
        pending_bytes = 0
        flush_deadline = asyncio.get_running_loop().time() + 0.1
        last_received = asyncio.get_running_loop().time()
        idle_timeout = float(self.task.get("logIdleTimeoutSeconds", 10))
        try:
            assert self._connection is not None
            while self._accepting_commands or getattr(self._connection, "has_buffered_data", False):
                try:
                    # 已有待写数据时按原批截止时间缩短等待，避免末尾小包再延长整个窗口。
                    read_timeout = min(.1, max(.001, flush_deadline - asyncio.get_running_loop().time())) if pending else .1
                    data = await asyncio.wait_for(self._connection.read(), timeout=read_timeout)
                except TimeoutError:
                    if pending:
                        to_flush, pending = pending, []
                        pending_bytes = 0
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
                except asyncssh.DisconnectError as error:
                    # AsyncSSH 的断线异常不是 OSError；传输已在 finally 关闭，允许运行时重连。
                    await _call(
                        self._on_state,
                        "READ_ERROR",
                        {"taskId": self.task_id, "sessionId": self.session_id, "error": str(error)},
                    )
                    break
                if not data:
                    break
                last_received = asyncio.get_running_loop().time()
                self._commands.observe_received(data)
                received_at = datetime.now(UTC)
                self.write_latency.begin()
                prefixed = self._prefixer.prefix(data, received_at)
                pending.append((prefixed, received_at))
                pending_bytes += len(prefixed)
                if pending_bytes >= 256 * 1024 or asyncio.get_running_loop().time() >= flush_deadline:
                    to_flush, pending = pending, []
                    pending_bytes = 0
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
                await self.stop_coredump_monitor(connection_usable=False)
                await self._close_connection()
                if pending:
                    to_flush, pending = pending, []
                    pending_bytes = 0
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
                    self._commands.fail_queued(ConnectionError("connection closed before command dispatch"))
                    self._sender.cancel()
                self._closed.set()
                await _call(
                    self._on_state, "CLOSED", {"taskId": self.task_id, "sessionId": self.session_id}
                )
            if cleanup_error:
                self._terminal_error = cleanup_error
                raise cleanup_error

    async def _flush(self, pending: list[tuple[bytes, object]]) -> None:
        if not pending:
            return
        self.write_latency.begin()
        positions = await self._writer.write_many(pending)
        self.write_latency.finish()
        for position in positions:
            data, received_at = pending[position.source_index]
            piece = data[position.source_offset:position.source_offset + position.length]
            await _call(
                self._on_log,
                LogChunk(
                    self.task_id,
                    self.run_id,
                    self.session_id,
                    position.sequence,
                    piece,
                    position.offset,
                    str(position.path),
                ),
            )
            if position.rollback_from is not None:
                await _call(self._on_state, "CLOCK_ROLLBACK", {
                    "taskId": self.task_id, "sessionId": self.session_id,
                    "previousReceivedAt": position.rollback_from.isoformat(),
                    "receivedAt": received_at.astimezone(UTC).isoformat(),
                    "sequence": position.sequence, "path": str(position.path),
                })
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
        self._stopping_commands = True
        for task in self._scheduled:
            task.cancel()
        try:
            await self.stop_coredump_monitor()
        finally:
            await self._close_connection()
            if self._reader:
                # 先断开网络使 read 返回，再等待收尾落盘；禁止取消最终 flush/归档路径。
                await self._reader
            if self._sender:
                self._commands.fail_queued(ConnectionError("collector stopped before command dispatch"))
                self._sender.cancel()
                try:
                    await self._sender
                except asyncio.CancelledError:
                    pass
            self._closed.set()
        if self._terminal_error:
            raise self._terminal_error

    async def _close_connection(self) -> None:
        """序列化所有关闭调用；失败或取消不确认释放，后续收尾仍可重试。"""
        self._accepting_commands = False
        async with self._connection_close_lock:
            if self._connection is None or self._connection_closed:
                return
            self._commands.close_debug()
            await self._connection.close()
            self._connection_closed = True

    async def stop_coredump_monitor(self, *, connection_usable: bool = True) -> None:
        """先停止重挂载轮询，再用同一发送队列尽力卸载本会话的 NFS 目标。"""
        from .coredump_cleanup import stop_monitor

        await stop_monitor(self, connection_usable=connection_usable)

    async def wait_closed(self) -> None:
        await self._closed.wait()
