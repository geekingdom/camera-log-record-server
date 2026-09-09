"""在同一 MongoDB 事务内校验手动命令会话归属与有界队列准入。"""

from fastapi import HTTPException
from pymongo import ReturnDocument

from camera_logs.commands import reservation
from camera_logs.common.database import now
from camera_logs.common.ownership import owner_filter

MANUAL_QUEUE_LIMIT = 100


async def admit_manual(repo, task, identifier, body, actor_id):
    """原子提交一个固定 ID 的命令；并发准入通过写同一任务文档串行化。

    仅快照计数不能防止并发插入超额，因此必须先实际递增任务声明版本。
    驱动在写冲突后重试整个回调，重新观察队列和任务状态；设备发送不在
    此事务中。固定 ID 由幂等入口分配，提交结果未知时不得换 ID 补发。
    """
    async def commit(session):
        current = await repo.db.tasks.find_one_and_update(
            {**owner_filter(task), "sessionId": task["sessionId"], "status": "COLLECTING",
             "desiredState": "RUNNING", "resourceDeleted": {"$ne": True}},
            {"$inc": {"commandClaimVersion": 1}},
            return_document=ReturnDocument.AFTER, session=session,
        )
        if current is None:
            raise HTTPException(409, "任务会话或归属已变化，请刷新后重试")
        scope = {"taskId": current["id"], "runId": current["runId"],
                 "sessionId": current["sessionId"], "kind": "MANUAL"}
        queued = await repo.db.commands.count_documents({**scope, "status": "QUEUED"}, session=session)
        if queued >= MANUAL_QUEUE_LIMIT:
            raise HTTPException(429, "命令队列已满")
        document = {**body, **scope, "id": identifier, "actor": actor_id,
                    "status": "QUEUED", "createdAt": now()}
        await repo.db.commands.insert_one(document, session=session)
        return document

    return await reservation.reservation_transaction(repo, commit)
