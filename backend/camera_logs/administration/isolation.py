"""在副本集事务中提交管理员的外部隔离确认，不把失联当作已释放连接。"""

import logging
from datetime import UTC, timedelta

from fastapi import HTTPException
from pymongo.errors import PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from camera_logs.collection.ssh_admission import release_task_slots
from camera_logs.common.database import now
from camera_logs.common.ownership import owner_filter

logger = logging.getLogger(__name__)


async def isolation_transaction(repo, callback):
    """事务冲突由驱动重试；不支持事务的部署必须失败，禁止降级为分步写入。"""
    async with repo.db.client.start_session() as session:
        return await session.with_transaction(callback, read_concern=ReadConcern("snapshot"),
                                             write_concern=WriteConcern("majority"))


async def confirm_node_isolation(repo, node_id, actor, evidence):
    """原子更新节点、任务、运行锁和审计，保留暂停及重连的原运行预算。"""
    async def commit(session):
        db = repo.db
        node = await db.nodes.find_one({"id": node_id}, session=session)
        if node is None:
            raise HTTPException(404, "节点不存在")
        heartbeat = node.get("heartbeat")
        if heartbeat is None or heartbeat.replace(tzinfo=UTC) > now() - timedelta(seconds=30):
            raise HTTPException(409, "节点仍在发送心跳或尚无实例记录，不能确认隔离")
        # 节点心跳与任务控制的并发写入会造成事务冲突；重试必须重新判断条件。
        await db.nodes.update_one({"id": node_id}, {"$set": {"accepting": False, "isolated": True}}, session=session)
        async for task in db.tasks.find({"nodeId": node_id, "status": "BLOCKED"}, session=session):
            # 管理员已确认旧节点完成外部隔离，才能归还旧运行的SSH名额；
            # 与节点/任务/审计共用事务，后续失败必须同时回滚，不能按失联时间释放。
            if task.get("protocol") == "SSH":
                await release_task_slots(repo, task, session=session)
            run_id = task.get("runId")
            stopped = task["desiredState"] == "STOPPED"
            restart = stopped and task.get("restartRequested") and not task.get("resourceDeleted")
            if stopped:
                await db.endpoint_locks.delete_one({"taskId": task["id"], "runId": run_id}, session=session)
                await db.runs.update_one({"id": run_id, "taskId": task["id"]},
                                         {"$set": {"endedAt": now()}}, session=session)
            # PAUSED+RUNNING 由既有恢复领取路径迁移，保留 runId 和定时预算。
            await db.tasks.update_one(owner_filter(task) | {"status": "BLOCKED"}, {"$set": {
                "nodeId": None, "status": "STOPPED" if stopped else "PAUSED", "error": None,
                "updatedAt": now(), **({"desiredState": "RUNNING", "restartRequested": False}
                                       if restart else {})}}, session=session)
            for previous, resulting in (("SENDING", "UNKNOWN"), ("QUEUED", "CANCELLED")):
                await db.commands.update_many({"taskId": task["id"], "runId": run_id, "status": previous},
                    {"$set": {"status": resulting, "completedAt": now()}}, session=session)
            if task["desiredState"] in ("PAUSED", "STOPPED"):
                await db.operations.update_many({"taskId": task["id"], "desiredState": task["desiredState"],
                    "status": "PENDING"}, {"$set": {"status": "SUCCEEDED", "completedAt": now()}}, session=session)
        await db.events.insert_one({"nodeId": node_id, "type": "EXTERNAL_FENCING_CONFIRMED",
            "actor": actor, "evidence": evidence, "createdAt": now()}, session=session)
        await repo.audit(actor, "confirm_node_isolation", node_id, session=session)
        return {"nodeId": node_id, "status": "ISOLATED"}

    try:
        return await isolation_transaction(repo, commit)
    except PyMongoError as error:
        logger.exception("节点隔离确认事务失败 node=%s", node_id)
        raise HTTPException(503, "隔离确认事务未完成，请查询节点状态后重试") from error
