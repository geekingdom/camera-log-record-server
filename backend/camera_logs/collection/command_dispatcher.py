"""单会话命令派发器：维护 FIFO、提示符等待和 PSH 调试握手。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from .contracts import AsyncConnection, Callback
from .contracts import call_callback as _call
from .psh_dialogue import PshDialogue, PshSwitchError


class BudgetExhausted(RuntimeError):
    """定时命令在写入 socket 前被持久化执行预算拒绝。"""


class CommandChannelBlocked(RuntimeError):
    """PSH 恢复未确认，禁止把普通命令写入可能仍等待口令的设备。"""


CommandItem = tuple[int, int, str, str, str | None, float, Callback | None, Callback | None, bool, asyncio.Future[None]]


class CommandDispatcher:
    """绑定一个采集会话的唯一命令发送队列，不直接拥有连接生命周期。"""

    def __init__(
        self,
        task: Mapping[str, Any],
        *,
        resolve_debug_password: Callback | None,
        on_debug: Callback | None,
        connection: Callable[[], AsyncConnection | None],
        accepting: Callable[[], bool],
        stopping: Callable[[], bool],
        closed: Callable[[], bool],
    ) -> None:
        self._task = task
        self._connection, self._accepting = connection, accepting
        self._stopping, self._closed = stopping, closed
        self._debug = PshDialogue(task, resolve_debug_password, on_debug)
        self._queue: asyncio.PriorityQueue[CommandItem] = asyncio.PriorityQueue()
        self._counter = 0
        self._command_blocked = False
        self._received_epoch = 0
        self._prompt_waiter: tuple[bytes, asyncio.Future[None], int] | None = None
        self._prompt_tail = b""
        self.sending_future: asyncio.Future[None] | None = None

    async def enqueue(
        self,
        command: str,
        newline: str,
        *,
        priority: int,
        prompt: str | None = None,
        timeout_seconds: float = 30,
        before_send: Callback | None = None,
        session_guard: Callback | None = None,
        allow_during_shutdown: bool = False,
    ) -> None:
        """按优先级和入队序号串行提交命令，失败始终结束本命令等待者。"""
        if not allow_during_shutdown and (not self._accepting() or self._stopping() or self._closed()):
            raise ConnectionError("connection closed")
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._counter += 1
        await self._queue.put((priority, self._counter, command, newline, prompt, timeout_seconds,
                               before_send, session_guard, allow_during_shutdown, future))
        await future

    def observe_received(self, data: bytes) -> None:
        """由唯一接收器按字节顺序调用，供 PSH 对话和提示符等待器消费。"""
        self._received_epoch += 1
        self._debug.feed(data)
        if self._prompt_waiter:
            needle, waiter, after_epoch = self._prompt_waiter
            combined = self._prompt_tail + data
            if self._received_epoch > after_epoch and needle in combined and not waiter.done():
                waiter.set_result(None)
            self._prompt_tail = combined[-(len(needle) - 1):] if len(needle) > 1 else b""

    async def observe_initial_mode(self) -> None:
        """初始化 debug 前观察当前设备 shell，ASH 已确认时不重复发送口令。"""
        await self._debug.observe_initial_mode()

    async def write_debug(self, data: bytes) -> None:
        """调试握手逐片发送串口口令，关闭中的会话禁止继续输入。"""
        serial = (self._task.get("protocol") or self._task.get("protocolType")) == "TELNET_SERIAL"
        interval = float(self._task.get("pshSerialCharacterInterval", .1)) if serial else 0
        pieces = [bytes([value]) for value in data] if interval else [data]
        for index, piece in enumerate(pieces):
            if index and interval:
                await asyncio.sleep(interval)
            connection = self._connection()
            if self._stopping() or not self._accepting() or self._closed() or connection is None:
                raise PshSwitchError("PSH 切换会话已经关闭")
            await connection.write(piece)

    async def sender_loop(self) -> None:
        """独占连接写入，重检会话归属与预算后才发送业务字节。"""
        while True:
            _, _, command, newline, prompt, timeout_seconds, before_send, session_guard, allow_during_shutdown, future = await self._queue.get()
            self.sending_future = future
            prompt_future: asyncio.Future[None] | None = None
            try:
                if future.cancelled():
                    continue
                connection = self._connection()
                if connection is None or self._closed() or ((not self._accepting() or self._stopping()) and not allow_during_shutdown):
                    raise ConnectionError("connection closed")
                if session_guard:
                    await _call(session_guard)
                if future.cancelled():
                    continue
                if self._stopping() and not allow_during_shutdown:
                    raise ConnectionError("connection is stopping")
                if self._command_blocked:
                    # 恢复握手同样占用唯一发送槽；未回到 shell 时本次未进入业务发送阶段，
                    # 因而不能消耗定时预算，也不影响接收器继续保存设备日志。
                    if not await self._debug.recover_command_channel(self.write_debug, newline, timeout_seconds):
                        raise CommandChannelBlocked("PSH 调试恢复未确认，当前会话暂不可发送命令")
                    self._command_blocked = False
                if future.cancelled():
                    continue
                if session_guard:
                    # PSH 恢复可能耗时，业务字节写入前再次确认原会话仍归属当前任务。
                    await _call(session_guard)
                if future.cancelled():
                    continue
                if self._stopping() and not allow_during_shutdown:
                    raise ConnectionError("connection is stopping")
                if before_send and not await _call(before_send):
                    # 进入预算回调前取消不占用；回调已开始的持久预留不退回，但取消后禁写 socket。
                    raise BudgetExhausted("scheduled command budget is exhausted")
                if future.cancelled():
                    continue
                if self._stopping() and not allow_during_shutdown:
                    raise ConnectionError("connection is stopping")
                if command.strip() == "debug":
                    # 一次 debug、解密和口令输入作为一个队列项，后续命令不能穿插为口令。
                    await self._debug.ensure_ash(self.write_debug, newline, timeout_seconds)
                    self._command_blocked = False
                elif prompt:
                    prompt_future = asyncio.get_running_loop().create_future()
                    self._prompt_waiter = (prompt.encode(), prompt_future, self._received_epoch)
                    self._prompt_tail = b""
                if command.strip() != "debug":
                    await connection.write((command + newline).encode())
                if prompt and command.strip() != "debug":
                    await asyncio.wait_for(prompt_future, timeout_seconds)
                if not future.done():
                    future.set_result(None)
            except Exception as error:  # noqa: BLE001 - 每个发送失败都必须结束对应等待 future。
                if not future.done():
                    future.set_exception(error)
                if isinstance(error, PshSwitchError):
                    self._command_blocked = not self._debug.command_safe
            finally:
                if self._prompt_waiter and self._prompt_waiter[1] is prompt_future:
                    if prompt_future is not None and not prompt_future.done():
                        prompt_future.cancel()
                    self._prompt_waiter = None
                    self._prompt_tail = b""
                self.sending_future = None
                self._queue.task_done()

    def fail_queued(self, error: Exception) -> None:
        """连接关闭时结束正在发送和仍排队的命令，不能遗留等待 future。"""
        if self.sending_future and not self.sending_future.done():
            self.sending_future.set_exception(error)
        while True:
            try:
                *_, future = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not future.done():
                future.set_exception(error)
            self._queue.task_done()

    def close_debug(self) -> None:
        """关闭对话观察器，后继会话必须重新执行自己的 debug 探测。"""
        self._debug.close()
