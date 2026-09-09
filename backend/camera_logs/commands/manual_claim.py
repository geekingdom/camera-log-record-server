"""以任务归属事务领取手动命令，阻止旧运行实例向已接管会话发送。"""

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from camera_logs.commands import reservation
from camera_logs.common.database import now
from camera_logs.common.ownership import owner_filter


class ManualClaimUncertain(ConnectionError):
    """手动命令领取提交结果未知；调用方不得向设备发送，并仅可保守标记未知。"""


def _scope(task, record):
    """构造一条命令不可变会话身份，所有状态迁移都必须带上完整范围。"""
    return {
        "id": record["id"],
        "taskId": task["id"],
        "kind": "MANUAL",
        "runId": record.get("runId"),
        "sessionId": record.get("sessionId"),
    }


async def claim_manual(repo, task, session_id, record):
    """在同一事务中确认任务归属并将当前会话命令领取为 ``SENDING``。

    任务文档递增 ``commandClaimVersion`` 使同一任务上的停止、接管和其他命令领取
    与本次读取冲突，MongoDB 会重试整个事务回调。设备 socket 不在事务内；提交
    结果未知时抛出专用异常，调用方不能因为重试领取而重复发送。
    """
    record_scope = _scope(task, record)

    async def commit(session):
        current = await repo.db.tasks.find_one_and_update(
            {
                **owner_filter(task),
                "sessionId": session_id,
                "status": "COLLECTING",
                "desiredState": "RUNNING",
                "resourceDeleted": {"$ne": True},
            },
            {"$inc": {"commandClaimVersion": 1}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if current is None:
            return None

        # 旧会话命令只可在确认当前实例仍归属后取消；不能让已失属实例改写后继记录。
        if record.get("runId") != current["runId"] or record.get("sessionId") != current["sessionId"]:
            await repo.db.commands.update_one(
                {**record_scope, "status": "QUEUED"},
                {"$set": {"status": "CANCELLED", "completedAt": now()}},
                session=session,
            )
            return None

        scope = {
            "id": record["id"],
            "taskId": current["id"],
            "kind": "MANUAL",
            "runId": current["runId"],
            "sessionId": current["sessionId"],
        }
        return await repo.db.commands.find_one_and_update(
            {**scope, "status": "QUEUED"},
            {"$set": {"status": "SENDING", "startedAt": now()}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )

    try:
        return await reservation.reservation_transaction(repo, commit)
    except PyMongoError as error:
        raise ManualClaimUncertain("手动命令领取结果未知，本次未发送") from error
