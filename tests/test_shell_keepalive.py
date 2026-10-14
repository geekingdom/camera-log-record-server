"""会话内 shell 保活的调度边界：异常和不可写状态必须有等待，不得占满事件循环。"""

import asyncio
import logging
import time

import pytest
from camera_logs.collection.shell_keepalive import ShellKeepalive


@pytest.mark.parametrize("ready, error", [(False, None), (True, ConnectionError("closed"))])
async def test_shell_keepalive_unavailable_or_failed_enqueue_always_backs_off(monkeypatch, ready, error):
    """会话不可写及重复入队异常都必须睡眠一个有限间隔，不能在到期计时下空转。"""
    closed = False
    waits: list[float] = []
    attempts = 0

    async def sleep(seconds):
        nonlocal closed
        waits.append(seconds)
        if len(waits) == 3:
            closed = True

    async def enqueue():
        nonlocal attempts
        attempts += 1
        if error:
            raise error

    monkeypatch.setattr(asyncio, "sleep", sleep)
    keepalive = ShellKeepalive(
        interval_seconds=lambda: .01,
        last_command_monotonic=lambda: time.monotonic() - 1,
        closed=lambda: closed,
        ready=lambda: ready,
        enqueue=enqueue,
        task_id="task",
        session_id="session",
        logger=logging.getLogger(__name__),
    )

    await keepalive.run()

    assert waits == [.01, .01, .01]
    assert attempts == (0 if not ready else 3)
