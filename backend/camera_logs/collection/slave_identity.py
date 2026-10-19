"""从机运行时身份巡检：复用当前会话确认 shell 未回落至外层主机。"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any

from .command_dispatcher import CommandChannelBlocked
from .slave_shell import matches_slave_identity

SLAVE_IDENTITY_CHECK_SECONDS = 60
SLAVE_IDENTITY_CHECK_TIMEOUT_SECONDS = 10
_MARKER_PREFIX = "__CAMERA_LOGS_SLAVE_IDENTITY_"
logger = logging.getLogger(__name__)


def is_slave_ssh_task(task: Mapping[str, Any]) -> bool:
    """仅已通过嵌套 SSH 交接的三类从机需要持续确认当前 shell 身份。"""
    return task.get("protocol") == "SSH" and task.get("sshTarget") in {
        "SLAVE_1", "SLAVE_2", "SLAVE_3",
    }


def probe_output_after_marker(output: bytes, marker: str) -> bytes | None:
    """取随机起始标记后的本次命令输出，兼容 SSH PTY 的 LF、CRLF 和跨包拼接。"""
    boundary = re.compile(rb"(?:^|\r?\n)" + re.escape(marker.encode()) + rb"\r?\n")
    matched = boundary.search(output)
    return output[matched.end():] if matched else None


async def run_slave_identity_probe(collector: Any) -> None:
    """每分钟串行捕获 ifconfig；无法证明目标身份时关闭会话供运行时完整重连。"""
    while not collector._closed.is_set():
        await asyncio.sleep(SLAVE_IDENTITY_CHECK_SECONDS)
        if collector._closed.is_set():
            return
        try:
            if not collector._verified_output_target() or not collector._commands.can_send_shell_keepalive():
                raise CommandChannelBlocked("从机身份巡检命令通道未确认")
            marker = _MARKER_PREFIX + uuid.uuid4().hex + "__"
            output = await collector.capture_monitor_command(
                f'printf "\\n{marker}\\n"; ifconfig',
                session_guard=lambda: _guard_current_slave(collector),
                timeout_seconds=SLAVE_IDENTITY_CHECK_TIMEOUT_SECONDS,
            )
            identity_output = probe_output_after_marker(output, marker)
            matched = identity_output is not None and matches_slave_identity(collector.task, identity_output)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 无法确认当前 shell 时同样不能继续采集。
            logger.warning("从机身份巡检失败，关闭会话等待重连 task=%s session=%s errorType=%s",
                           collector.task_id, collector.session_id, type(error).__name__)
            matched = False
        if matched:
            continue
        logger.warning("从机身份巡检未匹配目标，关闭会话等待重连 task=%s session=%s",
                       collector.task_id, collector.session_id)
        await collector._close_connection()
        return


def _guard_current_slave(collector: Any) -> None:
    """发送前重检本会话仍是已验证从机 ASH，排队期间身份变化不得写入 shell。"""
    if not collector._verified_output_target() or not collector._commands.can_send_shell_keepalive():
        raise CommandChannelBlocked("从机身份巡检命令通道未确认")
