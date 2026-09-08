"""按会话分发手动命令，并有界清理重连后迟到的旧排队记录。"""

import logging

from camera_logs.common.database import now
from camera_logs.common.ownership import owner_filter

logger = logging.getLogger(__name__)


async def next_manual_command(repo, runtime):
    """清理前复核归属，只取消快照中的旧记录；当前会话独立按 FIFO 选择。"""
    collector = getattr(runtime, "collector", None)
    if collector is None or runtime.stopping or getattr(runtime, "retired", False):
        return None
    task_id, run_id, session_id = runtime.task["id"], runtime.task["runId"], collector.session_id
    queued = {"taskId": task_id, "kind": "MANUAL", "status": "QUEUED"}
    stale = [item async for item in repo.db.commands.find({**queued, "$or": [
        {"runId": {"$ne": run_id}}, {"sessionId": {"$ne": session_id}},
    ]}, {"id": 1, "runId": 1, "sessionId": 1}).sort([("createdAt", 1), ("id", 1)]).limit(100)]
    current = await repo.db.tasks.find_one({**owner_filter(runtime.task), "sessionId": session_id,
                                          "status": "COLLECTING", "desiredState": "RUNNING"})
    if current is None or runtime.collector is not collector or runtime.stopping:
        return None
    if stale:
        # 禁止直接用“不等于当前会话”批量更新：等待数据库期间可能出现后继会话。
        # 已取得的 ID 与原会话共同约束更新，新插入的记录不在本次清理范围内。
        result = await repo.db.commands.update_many({**queued, "$or": [
            {"id": item["id"], "runId": item.get("runId"), "sessionId": item.get("sessionId")}
            for item in stale
        ]}, {"$set": {"status": "CANCELLED", "completedAt": now()}})
        if result.modified_count:
            logger.info("取消过期会话手动命令 task=%s count=%s", task_id, result.modified_count)
    return await repo.db.commands.find_one({**queued, "runId": run_id, "sessionId": session_id},
                                            sort=[("createdAt", 1), ("id", 1)])
