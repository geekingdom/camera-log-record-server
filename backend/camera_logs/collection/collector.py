"""单路采集器：单连接接收、FIFO 原始字节写入和串行命令发送。

本模块保留重复正文，仅按约定添加上海时区行首时间戳，不混入命令审计文本；每个
实例只处理一个 task/run/session。连接断开、空闲超时和人工停止均由运行时决定是否重连。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncssh

from camera_logs.common.write_metrics import WriteLatency
from camera_logs.logs.storage import HourlyWriter

from . import buffering
from .command_dispatcher import BudgetExhausted, CommandChannelBlocked, CommandDispatcher
from .contracts import AsyncConnection, Callback, LogChunk
from .contracts import call_callback as _call
from .line_prefix import LinePrefixer
from .output_recovery import OutputRecovery, OutputRecoveryCommandSkipped, supports_output_recovery
from .psh_dialogue import PshSwitchError
from .scheduled_supervision import supervise_scheduled
from .shell_keepalive import ShellKeepalive
from .slave_identity import is_slave_ssh_task, run_slave_identity_probe

_SHELL_KEEPALIVE_SECONDS = 60
_OUTPUT_CLOSE_TIMEOUT_SECONDS = 2
_OUTPUT_RECOVERY_LEASE_TIMEOUT_SECONDS = 2
_LOGREAD_RECOVERY_COMMAND = "kill $(ps | grep logread | grep -v \"logread -n\" | grep -v grep | awk '{print $1}')"
logger = logging.getLogger(__name__)


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
        on_receive: Callback | None = None,
        on_liveness_reset: Callback | None = None,
        claim_output_recovery: Callback | None = None,
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
            on_archive=self._on_archive,
        )
        self.write_latency = WriteLatency()
        # 总延迟保留批处理与文件锁等待；实际I/O只统计write_thread文件操作，
        # 防止归档排队被误当成磁盘压力，触发进一步降速。
        self.write_io_latency = WriteLatency()
        self._buffer_reservation = buffering.BufferReservation(f"{self.task_id}/{self.session_id}")
        self._sender: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._scheduled: list[asyncio.Task[None]] = []
        self._shell_keepalive: asyncio.Task[None] | None = None
        self._slave_identity_check: asyncio.Task[None] | None = None
        self._coredump_monitor: asyncio.Task[None] | None = None
        self._coredump_cleanup: Any | None = None
        self._coredump_report: Callback | None = None
        self._closed = asyncio.Event()
        self._accepting_commands = True
        self._stopping_commands = False
        self._initializing = True
        self._protocol_initializing = False
        self._command_status: dict[str, str] = {}
        self._prefixer = LinePrefixer()
        self._terminal_error: Exception | None = None
        # 存活判定只观察实际设备字节。时间在读协程取得字节后立刻记录，不能等待
        # 批量写盘或目录回调，否则慢磁盘会被上层错误判断为设备静默。
        self.last_received_monotonic: float | None = None
        self.liveness_read_observation_started = time.monotonic()
        self.liveness_waiting_for_read = False
        self.liveness_storage_busy = False
        self._on_receive = on_receive
        self._on_liveness_reset = on_liveness_reset
        self._claim_output_recovery = claim_output_recovery
        self._commands = CommandDispatcher(
            self.task | {"id": self.task_id, "runId": self.run_id, "sessionId": self.session_id},
            resolve_debug_password=resolve_debug_password,
            on_debug=on_debug,
            connection=lambda: self._connection,
            accepting=lambda: self._accepting_commands,
            stopping=lambda: self._stopping_commands,
            closed=lambda: self._connection_closed,
            close_failed_slave=self._close_failed_slave,
            on_command_written=self._observe_shell_command,
        )
        self._last_shell_command_monotonic = time.monotonic()
        self._output_close_attempted = False
        self._output_recovery = OutputRecovery(
            self.task,
            closed=self._closed.is_set,
            ready=self._output_recovery_ready,
            recover=self._recover_logread_output,
            claim_lease=self._claim_output_recovery_lease if claim_output_recovery else None,
            close_on_lease_failure=self._close_connection,
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
        try:
            slave = self.task.get("protocol") == "SSH" and self.task.get("sshTarget") in {
                "SLAVE_1", "SLAVE_2", "SLAVE_3",
            }
            # 从机工厂必须先完成目标IP验证，未确认前不启动reader，禁止主机输出落盘。
            if slave:
                await self._commands.accept_verified_slave(connection)
            self._protocol_initializing = not slave and self.task.get("protocol") in {"SSH", "TELNET_DEVICE"}
            self.liveness_read_observation_started = time.monotonic()
            self._sender = asyncio.create_task(self._sender_loop())
            self._reader = asyncio.create_task(self._reader_loop())
            if not slave and self.task.get("protocol") in {"SSH", "TELNET_DEVICE"}:
                await self._commands.observe_initial_mode()
                try:
                    await self._commands.ensure_ash()
                except (PshSwitchError, CommandChannelBlocked):
                    # 主机/设备Telnet尽力切换：失败已由对话记录，继续初始化及接收。
                    # 未退出密码提示时沿用发送队列的恢复保护，不将普通命令误作口令。
                    pass
            elif not slave and any(str(item["command"]).strip() == "debug" for item in self.task.get("initialCommands", [])):
                await self._commands.observe_initial_mode()
            self._protocol_initializing = False
            self.liveness_read_observation_started = time.monotonic()
            for item in self.task.get("initialCommands", []):
                await self._send_initial(item)
            self._initializing = False
            # 定时预算要求当前状态已持久化；发布完成后再开始首次间隔计时。
            await _call(
                self._on_state, "COLLECTING", {"taskId": self.task_id, "sessionId": self.session_id}
            )
            for position, item in enumerate(self.task.get("scheduledCommands", [])):
                self._scheduled.append(asyncio.create_task(supervise_scheduled(self, position, item)))
            self._start_shell_keepalive()
            self._start_slave_identity_check()
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

    def _supports_shell_keepalive(self) -> bool:
        """只向需要交互 shell 的网络会话写保活，串口和传输层保活保持独立。"""
        return (self.task.get("protocol") or self.task.get("protocolType")) in {"SSH", "TELNET_DEVICE"}

    def _observe_shell_command(self, _command: str) -> None:
        """仅在发送队列实际写入普通 shell 命令后刷新保活计时，排队不算活动。"""
        self._last_shell_command_monotonic = time.monotonic()

    def _start_shell_keepalive(self) -> None:
        """初始化完成后启动会话内保活；从机同样复用其已经验证的单一 shell。"""
        if self._supports_shell_keepalive() and self._shell_keepalive is None:
            keepalive = ShellKeepalive(
                interval_seconds=lambda: _SHELL_KEEPALIVE_SECONDS,
                last_command_monotonic=lambda: self._last_shell_command_monotonic,
                closed=self._closed.is_set,
                ready=self._shell_keepalive_ready,
                enqueue=self._enqueue_shell_keepalive,
                task_id=self.task_id,
                session_id=self.session_id,
                logger=logger,
            )
            self._shell_keepalive = asyncio.create_task(keepalive.run())

    def _shell_keepalive_ready(self) -> bool:
        """生成保活前检查会话仍可写，不能让保活触发 PSH 恢复或建立额外连接。"""
        return (
            not self._stopping_commands
            and self._accepting_commands
            and self._connection is not None
            and self._commands.can_send_shell_keepalive()
        )

    async def _enqueue_shell_keepalive(self) -> None:
        """通过唯一发送队列写 shell 空操作，保持接收器和命令顺序不变。"""
        # `:` 是 POSIX shell 内建空操作，不读取设备状态、无输出，也不占业务预算。
        await self._enqueue(":", "\n", priority=3, timeout_seconds=10,
                            session_guard=self._guard_shell_keepalive)

    def _guard_shell_keepalive(self) -> None:
        """发送队列取出保活时再次核对调试状态，防止排队期间进入未知 PSH。"""
        if time.monotonic() - self._last_shell_command_monotonic < _SHELL_KEEPALIVE_SECONDS:
            raise CommandChannelBlocked("已有普通 shell 输入，跳过过期保活")
        if not self._commands.can_send_shell_keepalive():
            raise CommandChannelBlocked("PSH 命令通道尚未确认，跳过 shell 保活")

    async def _stop_shell_keepalive(self) -> None:
        """关闭入口统一取消保活，避免停止后仍有排队输入。"""
        task, self._shell_keepalive = self._shell_keepalive, None
        if task:
            task.cancel()
            if task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)

    def _start_slave_identity_check(self) -> None:
        """从机每分钟复用当前会话核对 ifconfig，禁止另建连接或接收器。"""
        if is_slave_ssh_task(self.task) and self._slave_identity_check is None:
            self._slave_identity_check = asyncio.create_task(run_slave_identity_probe(self))

    async def _stop_slave_identity_check(self) -> None:
        """关闭会话时取消身份巡检，避免连接回收后遗留排队 shell 输入。"""
        task, self._slave_identity_check = self._slave_identity_check, None
        if task:
            task.cancel()
            if task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)

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
        require_safe_channel: bool = False,
    ) -> None:
        try:
            await self._commands.enqueue(
                command, newline, priority=priority, prompt=prompt, timeout_seconds=timeout_seconds,
                before_send=before_send, session_guard=session_guard,
                allow_during_shutdown=allow_during_shutdown,
                require_safe_channel=require_safe_channel,
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

    def _output_recovery_ready(self) -> bool:
        """恢复命令仅能在当前网络 shell 已确认、会话仍可写时进入队列。"""
        return (
            not self._stopping_commands and self._accepting_commands and self._connection is not None
            and self._commands.can_send_shell_keepalive() and self._verified_output_target()
        )

    async def _claim_output_recovery_lease(self) -> bool:
        """有界领取资源 CAS；超时不可确认时关闭会话，不能重复执行 kill。"""
        return bool(await asyncio.wait_for(
            _call(self._claim_output_recovery), timeout=_OUTPUT_RECOVERY_LEASE_TIMEOUT_SECONDS,
        ))

    def _verified_output_target(self) -> bool:
        """SSH 从机仅在引导器已验证本目标身份后允许控制其自身输出。"""
        if self.task.get("protocol") != "SSH" or self.task.get("sshTarget", "HOST") == "HOST":
            return True
        return (
            getattr(self._connection, "bootstrap_verified", False) is True
            and getattr(self._connection, "verified_shell_mode", None) == "ASH"
            and getattr(self._connection, "verified_target", None) == self.task.get("sshTarget")
            and not getattr(self._connection, "identity_lost", False)
        )

    def _guard_output_recovery(self) -> None:
        """发送前二次确认，避免排队期间进入 PSH 或切换到关闭中的会话。"""
        if not self._output_recovery_ready():
            raise CommandChannelBlocked("输出恢复会话或 PSH 命令通道尚未确认")

    async def _recover_logread_output(self) -> None:
        """按发送队列顺序终止异常 logread 后重开输出；写入结果未知时不重试。"""
        try:
            await self._enqueue(
                _LOGREAD_RECOVERY_COMMAND, "\n", priority=3, timeout_seconds=10,
                session_guard=self._guard_output_recovery, require_safe_channel=True,
            )
        except CommandChannelBlocked as exc:
            # 发送器仅在 socket 写入前给出该拒绝，输出与采集会话均保持原状。
            raise OutputRecoveryCommandSkipped() from exc
        await self._enqueue(
            "outputOpen", "\n", priority=3, timeout_seconds=10,
            session_guard=self._guard_output_recovery, require_safe_channel=True,
        )

    async def _close_device_output(self) -> None:
        """人工暂停/停止前尽力关闭设备输出；超时或取消都不能阻塞连接回收。"""
        if (self._output_close_attempted or not supports_output_recovery(self.task)
                or self._connection_closed or self._sender is None or self._sender.done()):
            return
        self._output_close_attempted = True
        try:
            await asyncio.wait_for(
                self._enqueue(
                    "outputClose", "\n", priority=3, timeout_seconds=_OUTPUT_CLOSE_TIMEOUT_SECONDS,
                    session_guard=self._guard_output_close, allow_during_shutdown=True,
                    require_safe_channel=True,
                ),
                timeout=_OUTPUT_CLOSE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except (CommandChannelBlocked, ConnectionError, OSError, TimeoutError):
            # 关闭过程不重试：传输写入结果未知时不能把 outputClose 重复送往设备。
            return
        except Exception:
            logger.exception("关闭设备输出失败 task=%s session=%s", self.task_id, self.session_id)

    def _guard_output_close(self) -> None:
        """停止窗口只允许已确认的 shell 写 outputClose，不触发 PSH 自动恢复。"""
        if (self._connection is None or self._connection_closed or not self._commands.can_send_shell_keepalive()
                or not self._verified_output_target()):
            raise CommandChannelBlocked("输出关闭命令通道尚未确认")

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
            before_send_entered = reservation_attempted = send_authorized = False
            try:
                async def before_send(current_detail: dict[str, Any] = detail) -> bool:
                    """记录业务字节授权边界；无预算回调时也必须经此处确认才能写 socket。"""
                    nonlocal before_send_entered, reservation_attempted, send_authorized
                    before_send_entered = True
                    if self._reserve is None:
                        send_authorized = True
                        return True
                    reservation_attempted = True
                    send_authorized = bool(await _call(self._reserve, command_id, current_detail))
                    return send_authorized

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
                await _call(self._on_state, "SCHEDULED_COMMAND_UNSENT", detail | {
                    "commandId": command_id, "outcome": "FAILED",
                    "reason": "命令通道未确认，本次未发送",
                })
                continue
            except PshSwitchError as error:
                # PSH 切换是派发器在写入目标业务命令前的握手；即使预算已预留，仍可
                # 明确记为本次失败并让后续周期重新握手，不能关闭完整采集会话。
                execution += 1
                await _call(self._update, command_id, "FAILED", detail | {"error": str(error)})
                continue
            except (ConnectionError, OSError, TimeoutError) as error:
                await _call(
                    self._update,
                    command_id,
                    "UNKNOWN",
                    detail | {"error": str(error)},
                )
                # socket 或预留提交结果未知时必须由监督器关闭本会话；不能复用可能已污染的通道。
                raise
            except Exception as error:
                if not before_send_entered:
                    execution += 1
                    await _call(self._update, command_id, "FAILED", detail | {"error": str(error)})
                    # 尚未预留时没有执行记录可更新，另记事件保证入队故障可查询。
                    await _call(self._on_state, "SCHEDULED_COMMAND_UNSENT", detail | {
                        "commandId": command_id, "outcome": "FAILED",
                        "reason": "定时命令入队失败，本次未发送", "errorType": type(error).__name__,
                    })
                    continue
                # reservation_attempted 即使尚未返回也可能已持久化；send_authorized 后更可能已写 socket。
                boundary = "预算预留结果未知" if reservation_attempted and not send_authorized else "发送授权后结果未知"
                await _call(self._update, command_id, "UNKNOWN", detail | {"error": str(error), "boundary": boundary})
                raise

    async def _reader_loop(self) -> None:
        pending: list[tuple[bytes, object]] = []
        pending_bytes = 0
        flush_deadline = asyncio.get_running_loop().time() + buffering.batch_parameters()[1]
        idle_timeout = float(self.task.get("logIdleTimeoutSeconds", 10))
        try:
            assert self._connection is not None
            while self._accepting_commands or getattr(self._connection, "has_buffered_data", False):
                # 持有正文时先排空再等预算，避免多个会话互相等待造成死锁。
                if pending and not buffering.budget().available():
                    to_flush, pending = pending, []
                    pending_bytes = 0
                    await self._flush(to_flush)
                try:
                    # 已有待写数据时按原批截止时间缩短等待，避免末尾小包再延长整个窗口。
                    read_timeout = min(.1, max(.001, flush_deadline - asyncio.get_running_loop().time())) if pending else .1
                    self.liveness_waiting_for_read = True
                    try:
                        data = await self._read_budgeted(read_timeout)
                    finally:
                        self.liveness_waiting_for_read = False
                except TimeoutError:
                    if pending:
                        to_flush, pending = pending, []
                        pending_bytes = 0
                        await self._flush(to_flush)
                    await self._publish_archives()
                    self.liveness_storage_busy = True
                    try:
                        synced = await self._writer.sync_due()
                    finally:
                        self.liveness_storage_busy = False
                    if synced:
                        await self._reset_liveness_observation()
                    flush_deadline = asyncio.get_running_loop().time() + buffering.batch_parameters()[1]
                    # 协议心跳不算日志；真实存储阻塞结束后重新观察，避免把背压算成静默。
                    # 协议握手自带有界超时；解密等待不能触发日志空闲关闭整个连接。
                    if not self._protocol_initializing and time.monotonic() - self.liveness_read_observation_started >= idle_timeout:
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
                received_monotonic = time.monotonic()
                self.last_received_monotonic = received_monotonic
                self.liveness_read_observation_started = received_monotonic
                await _call(self._on_receive)
                self._commands.observe_received(data)
                self._output_recovery.observe(data)
                received_at = datetime.now(UTC)
                self.write_latency.begin()
                prefixed = self._prefixer.prefix(data, received_at)
                self._buffer_reservation.settle_read(len(prefixed))
                buffering.budget().pending.setdefault(id(self), received_monotonic)
                pending.append((prefixed, received_at))
                pending_bytes += len(prefixed)
                if pending_bytes >= buffering.batch_parameters()[0] or asyncio.get_running_loop().time() >= flush_deadline:
                    to_flush, pending = pending, []
                    pending_bytes = 0
                    await self._flush(to_flush)
                    flush_deadline = asyncio.get_running_loop().time() + buffering.batch_parameters()[1]
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
                await self._output_recovery.stop()
                await self.stop_coredump_monitor(connection_usable=False)
                await self._close_connection()
                if pending:
                    to_flush, pending = pending, []
                    pending_bytes = 0
                    await self._flush(to_flush)
                self.liveness_storage_busy = True
                try:
                    archive = await self._writer.close(defer=True)
                finally:
                    self.liveness_storage_busy = False
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
                self._buffer_reservation.release_all()
                buffering.budget().pending.pop(id(self), None)
                for task in self._scheduled:
                    task.cancel()
                await self._stop_shell_keepalive()
                await self._stop_slave_identity_check()
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

    async def _read_budgeted(self, timeout):
        """读前预留最坏前缀空间；背压等待不计设备静默，失败/EOF及时释放。"""
        waited = not buffering.budget().available()
        self.liveness_storage_busy = True
        try:
            await self._buffer_reservation.acquire_read()
        finally:
            self.liveness_storage_busy = False
        try:
            if waited:
                await self._reset_liveness_observation()
            data = await asyncio.wait_for(self._connection.read(buffering.READ_BYTES), timeout=timeout)
            if data:
                return data
        except BaseException:
            self._buffer_reservation.settle_read(0)
            raise
        self._buffer_reservation.settle_read(0)
        return data

    async def _flush(self, pending: list[tuple[bytes, object]]) -> None:
        if not pending:
            return
        self.liveness_storage_busy = True
        try:
            self.write_latency.begin()
            from camera_logs.logs.write_io import measure_io, record_stage
            started = buffering.budget().pending.get(id(self))
            if started is not None:
                record_stage("batchWaitMs", max(0, time.monotonic() - started) * 1000)
            with measure_io(self.write_io_latency):
                positions = await self._writer.write_many(pending)
            self.write_latency.finish()
        finally:
            self.liveness_storage_busy = False
            self._buffer_reservation.release_all()
            buffering.budget().pending.pop(id(self), None)
        await self._reset_liveness_observation()
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
                    "previousReceivedAt": position.rollback_from.astimezone(self._writer.zone).isoformat(),
                    "receivedAt": received_at.astimezone(self._writer.zone).isoformat(),
                    "sequence": position.sequence, "path": str(position.path),
                })
        await self._publish_archives()

    async def _reset_liveness_observation(self) -> None:
        """真实存储 I/O 结束后才重开静默观察，不能把背压时间算作设备无输出。"""
        self.liveness_read_observation_started = time.monotonic()
        await _call(self._on_liveness_reset, self.liveness_read_observation_started)

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
        await self._stop_shell_keepalive()
        await self._stop_slave_identity_check()
        try:
            await self._output_recovery.stop()
            await self.stop_coredump_monitor()
            await self._close_device_output()
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

    async def _close_failed_slave(self) -> None:
        """调试失败沿用统一关闭锁；关闭未知保留终结异常供运行时隔离，不能伪装释放。"""
        try:
            await self._close_connection()
        except Exception as error:  # noqa: BLE001 - 保留关闭异常供运行时隔离，不能从sender后台丢失。
            self._terminal_error = error

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
