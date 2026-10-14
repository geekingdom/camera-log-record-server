"""会话内 shell 保活：复用既有命令队列，避免设备交互 shell 因空闲自动登出。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .command_dispatcher import CommandChannelBlocked
from .psh_dialogue import PshSwitchError

_SHELL_KEEPALIVE_RETRY_SECONDS = 1


class ShellKeepalive:
    """按实际 shell 写入时间低频入队保活，所有不能发送的路径都进行有界等待。"""

    def __init__(
        self,
        *,
        interval_seconds: Callable[[], float],
        last_command_monotonic: Callable[[], float],
        closed: Callable[[], bool],
        ready: Callable[[], bool],
        enqueue: Callable[[], Awaitable[None]],
        task_id: str,
        session_id: str,
        logger: logging.Logger,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._last_command_monotonic = last_command_monotonic
        self._closed = closed
        self._ready = ready
        self._enqueue = enqueue
        self._task_id, self._session_id, self._logger = task_id, session_id, logger

    async def run(self) -> None:
        """等待正常命令空闲后发送 `:`；失败和会话不可写都不会形成零等待循环。"""
        while not self._closed():
            interval = self._interval_seconds()
            remaining = max(0, interval - (time.monotonic() - self._last_command_monotonic()))
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            if not self._ready():
                await asyncio.sleep(min(_SHELL_KEEPALIVE_RETRY_SECONDS, interval))
                continue
            try:
                await self._enqueue()
            except asyncio.CancelledError:
                raise
            except (CommandChannelBlocked, ConnectionError, OSError, TimeoutError, PshSwitchError):
                # 会话切换或关闭由既有生命周期处理；退避后重读最后一次真实写入时间。
                await asyncio.sleep(min(_SHELL_KEEPALIVE_RETRY_SECONDS, interval))
            except Exception:
                self._logger.exception("shell 保活入队失败 task=%s session=%s", self._task_id, self._session_id)
                await asyncio.sleep(min(_SHELL_KEEPALIVE_RETRY_SECONDS, interval))
