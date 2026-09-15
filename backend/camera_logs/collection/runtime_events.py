"""采集会话状态映射与可追踪运行事件。

该模块集中处理 Collector 回调的归属核验、状态转换和事件持久化。设备错误正文
与异常文本不能写入事件集合；仅保存平台可信的任务、运行、会话和节点身份以及
固定中文说明。事件库暂时故障不妨碍会话关闭和重连。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from camera_logs.common.database import now
from camera_logs.common.ownership import OwnershipLost, owner_filter

logger = logging.getLogger(__name__)

_RECONNECT_MESSAGES = {
    "READ_ERROR": "设备连接读取异常，正在重新连接",
    "IDLE_TIMEOUT": "未收到设备日志达到空闲超时阈值，准备重新连接",
}
_EVENT_WRITE_TIMEOUT_SECONDS = 1.0


async def record_debug_event(runtime: Any, event: str, details: dict[str, Any]) -> None:
    """保存调试状态，只有成功落库的同会话 ASH 确认才可去重。"""
    command_blocked = bool(details.get("commandBlocked", False))
    debug_error = details.get("debugError")
    session_id = runtime.collector.session_id
    confirmation = (session_id, details["mode"], debug_error, command_blocked)
    repeated = event == "ALREADY_ASH" and confirmation == getattr(runtime, "last_already_ash", None)
    if not repeated:
        await runtime.repo.db.events.insert_one({
            "taskId": runtime.task["id"], "runId": runtime.task["runId"],
            "nodeId": runtime.repo.settings.node_id, "sessionId": session_id,
            "type": "DEBUG_MODE", "phase": event, "mode": details["mode"],
            "commandBlocked": command_blocked, "debugError": debug_error, "createdAt": now(),
        })
        # 写入失败必须保留旧确认值，下次仍可尝试记录；其他阶段开启新的确认周期。
        runtime.last_already_ash = confirmation if event == "ALREADY_ASH" else None
    if not getattr(runtime, "retired", False):
        await runtime.repo.db.tasks.update_one(owner_filter(runtime.task), {
            "$set": {"shellMode": details["mode"], "debugPhase": event,
                     "commandBlocked": command_blocked, "debugError": debug_error, "updatedAt": now()},
        })
    logger.info("设备调试模式交互 task=%s phase=%s mode=%s", runtime.task["id"], event, details["mode"])


async def _current_owner(runtime: Any) -> bool:
    """确认回调仍属于当前运行，阻止已退役会话覆盖后继状态或制造事件。"""
    if getattr(runtime, "retired", False):
        return False
    try:
        current = await runtime.repo.db.tasks.find_one(
            {**owner_filter(runtime.task), "status": {"$ne": "BLOCKED"}}, {"id": 1},
        )
    except Exception:
        logger.exception("采集状态回调归属查询失败 task=%s", runtime.task["id"])
        return False
    return current is not None


async def record_runtime_event(runtime: Any, document: dict[str, Any]) -> None:
    """尽力写入运行事件，数据库故障只进入服务日志，不改变采集控制流。"""
    try:
        await asyncio.wait_for(
            runtime.repo.db.events.insert_one(document), timeout=_EVENT_WRITE_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("采集运行事件写入失败 task=%s type=%s", runtime.task["id"], document.get("type"))


async def _record_clock_rollback(runtime: Any, details: dict[str, Any]) -> None:
    """保留时钟回拨的顺序证据，但不覆盖当前采集状态。"""
    await record_runtime_event(runtime, {
        "type": "CLOCK_ROLLBACK", "taskId": runtime.task["id"], "runId": runtime.task["runId"],
        "sessionId": details["sessionId"], "nodeId": runtime.repo.settings.node_id,
        "previousReceivedAt": details["previousReceivedAt"], "receivedAt": details["receivedAt"],
        "sequence": details["sequence"], "fileId": runtime.file_id(details["path"]),
        "message": "服务器接收时间回拨，日志已另起片段，按块序号保持接收顺序", "createdAt": now(),
    })
    logger.warning("采集接收时间回拨", extra={"context": {
        "taskId": runtime.task["id"], "sessionId": details["sessionId"],
        "previousReceivedAt": details["previousReceivedAt"], "receivedAt": details["receivedAt"],
    }})


async def _record_reconnect_reason(runtime: Any, state: str, details: dict[str, Any]) -> None:
    """状态 CAS 成功后记录无敏感内容的读取/空闲原因。"""
    await record_runtime_event(runtime, {
        "type": state, "taskId": runtime.task["id"], "runId": runtime.task["runId"],
        "sessionId": details.get("sessionId"), "nodeId": runtime.repo.settings.node_id,
        "message": _RECONNECT_MESSAGES[state], "level": "WARNING", "outcome": "UNKNOWN", "createdAt": now(),
    })
    logger.warning("采集连接进入重连", extra={"context": {
        "taskId": runtime.task["id"], "runId": runtime.task["runId"],
        "sessionId": details.get("sessionId"), "state": state,
    }})


async def apply_runtime_state(runtime: Any, state: str, details: dict[str, Any]) -> None:
    """在有效归属内记录事件并映射任务状态，保留 BLOCKED 和归属隔离语义。"""
    if getattr(runtime, "retired", False):
        if state == "CONNECTING":
            raise OwnershipLost("运行实例已隔离，禁止重新连接")
        return
    collector = getattr(runtime, "collector", None)
    current_session = getattr(collector, "session_id", None) if collector is not None else None
    if current_session is not None and details.get("sessionId") != current_session:
        # 新会话已创建时，旧会话任何状态都不能覆盖新会话或写入误导性事件。
        return
    if state == "CLOCK_ROLLBACK":
        if not await _current_owner(runtime):
            return
        await _record_clock_rollback(runtime, details)
        return
    original_state = state
    if state in _RECONNECT_MESSAGES:
        state = "RECONNECTING"
    elif state == "CLOSED":
        state = "STOPPING" if runtime.stopping else "RECONNECTING"
    if state == "ARCHIVE_ERROR":
        await runtime.repo.db.tasks.update_one(
            {**owner_filter(runtime.task), "status": {"$ne": "BLOCKED"}},
            {"$set": {"archiveError": details.get("error"), "updatedAt": now()}},
        )
        return
    changed = await runtime.repo.db.tasks.update_one(
        {**owner_filter(runtime.task), "status": {"$ne": "BLOCKED"}},
        {"$set": {"status": state, "sessionId": details.get("sessionId"), "updatedAt": now()}},
    )
    if state == "CONNECTING" and not changed.matched_count:
        raise OwnershipLost("建连前任务归属或准入已失效")
    if not changed.matched_count:
        return
    if original_state in _RECONNECT_MESSAGES:
        await _record_reconnect_reason(runtime, original_state, details)
    if state == "COLLECTING" and changed.matched_count:
        await runtime.repo.db.tasks.update_one(owner_filter(runtime.task), {"$set": {"error": None}})
        await runtime.repo.db.operations.update_many(
            {"taskId": runtime.task["id"], "desiredState": "RUNNING", "status": "PENDING"},
            {"$set": {"status": "SUCCEEDED", "completedAt": now()}},
        )
    logger.info("采集状态变化", extra={"context": {"taskId": runtime.task["id"], "state": state}})
