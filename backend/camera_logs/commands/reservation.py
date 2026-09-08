"""以 MongoDB 事务预留定时命令预算和发送执行记录。"""

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.common.ownership import owner_filter


class _ReservationRejected(RuntimeError):
    """事务内的预算或命令条件失效，必须整体回滚后返回未预留。"""


class ReservationUncertain(ConnectionError):
    """数据库未确认预留结果；携带固定 ID 供发送器收尾，不授权设备写入。"""

    def __init__(self, execution_id):
        super().__init__("定时命令预留结果未知，本次未发送")
        self.execution_id = execution_id


async def reservation_transaction(repo, callback):
    """以快照读取和多数确认运行预留事务，供测试替换事务执行器。"""
    async with repo.db.client.start_session() as session:
        return await session.with_transaction(
            callback,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority", j=True),
        )


def _scheduled_command(task, command_id):
    """按配置标识查找定时命令，缺失时返回 ``None`` 而不创建预算。"""
    return next((command for command in task.get("scheduledCommands", []) if command["id"] == command_id), None)


async def reserve_scheduled(repo, task, command_id, session_id):
    """为当前采集会话原子扣减一次预算并创建 ``SENDING`` 执行记录。

    执行标识在事务回调外固定，驱动重试不会产生第二条执行记录。任务状态、运行
    归属和会话在回调内重新验证；取消、重复键和未知提交结果向上传播，调用方不能
    删除执行记录或退回预算，因为设备端是否收到命令可能已经不可知。
    """
    if _scheduled_command(task, command_id) is None:
        return None
    execution_id = new_id()

    async def commit(session):
        """在可重试事务回调中先声明任务所有权，再预留预算和执行记录。"""
        db = repo.db
        current = await db.tasks.find_one_and_update(
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

        command = _scheduled_command(current, command_id)
        if command is None:
            raise _ReservationRejected("定时命令配置在预留期间被删除")
        budget_id = f'{current["runId"]}:{command_id}'
        await db.budgets.update_one(
            {"_id": budget_id},
            {"$setOnInsert": {"attempts": 0}},
            upsert=True,
            session=session,
        )
        budget = await db.budgets.find_one_and_update(
            {"_id": budget_id, "attempts": {"$lt": command["totalExecutions"]}},
            {"$inc": {"attempts": 1}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if budget is None:
            raise _ReservationRejected("定时命令预算已耗尽")
        execution = {
            "id": execution_id,
            "taskId": current["id"],
            "runId": current["runId"],
            "sessionId": session_id,
            "commandId": command_id,
            "kind": "SCHEDULED",
            "attempt": budget["attempts"],
            "status": "SENDING",
            "createdAt": now(),
        }
        await db.commands.insert_one(execution, session=session)
        return execution

    try:
        return await reservation_transaction(repo, commit)
    except _ReservationRejected:
        return None
    except PyMongoError as error:
        raise ReservationUncertain(execution_id) from error
