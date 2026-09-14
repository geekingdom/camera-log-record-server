"""从机 SSH 引导的固定字段排障事件，不保存凭据、令牌、设备输出或异常正文。"""

import asyncio
import logging

from camera_logs.common.database import now

logger = logging.getLogger(__name__)
_EVENT_WRITE_TIMEOUT_SECONDS = 0.2


async def record_slave_bootstrap_event(repo, task, *, phase, outcome, level, message, port,
                                       host_task_id=None, host_node_id=None):
    """尽力记录从机引导阶段；持久化故障只写固定安全日志，绝不改变连接控制流。"""
    document = {
        "type": "SLAVE_SSH_BOOTSTRAP", "createdAt": now(),
        "taskId": task["id"], "runId": task["runId"], "generation": task["generation"],
        "resourceId": task["resourceId"], "sshTarget": task.get("sshTarget"), "nodeId": task.get("nodeId"),
        "port": port,
        "hostTaskId": host_task_id, "hostNodeId": host_node_id,
        "phase": phase, "outcome": outcome, "level": level, "message": message,
    }
    try:
        await asyncio.wait_for(repo.db.events.insert_one(document), timeout=_EVENT_WRITE_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 事件失败不得中止从机建连、清理或状态恢复。
        logger.warning("从机SSH引导事件写入失败 task=%s phase=%s", task["id"], phase)
