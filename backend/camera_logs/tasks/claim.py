"""在 MongoDB 事务中原子领取采集任务，避免取消留下孤立运行锁或运行记录。"""

from datetime import timedelta

from pymongo import ReturnDocument
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from camera_logs.common.config import DEFAULT_NODE_CAPACITY
from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.node.health import resource_pressure
from camera_logs.node.input_admission import input_rate_blocked
from camera_logs.node.resource_routing import accepts_resource
from camera_logs.node.write_pressure import write_latency_blocked


class SchedulerLeaseLost(RuntimeError):
    """事务开始时调度租约已失效，调用方必须结束本轮调度。"""


class _ClaimRejected(RuntimeError):
    """CAS 未命中时触发事务回滚，防止预先创建的运行锁单独提交。"""


def requires_coredump_nfs(task, resource):
    """仅资源级 Coredump 的可执行主机任务要求节点具备 NFS 能力。

    历史 SSH 任务缺少 ``sshTarget`` 时继续解释为主机；SSH 从机只采集其自身日志，
    不会持有资源级 Coredump 租约，因此不能因资源开关被错误排除在非 NFS 节点外。
    """
    if not resource or not resource.get("enableCoredumpMonitor") or resource.get("coredumpLeaseTarget"):
        return False
    if task.get("protocol") == "TELNET_DEVICE":
        return True
    return task.get("protocol") == "SSH" and task.get("sshTarget", "HOST") == "HOST"


async def claim_transaction(repo, callback):
    """以快照读取和多数确认提交一次领取事务，驱动可在冲突时重试回调。"""
    async with repo.db.client.start_session() as session:
        return await session.with_transaction(
            callback,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority", j=True),
        )


async def claim_task(repo, task, node_id, *, lease=None, occupied=0):
    """原子绑定任务、运行锁和运行记录；条件变化或 CAS 失败返回 ``None``。

    新运行的 ``run_id`` 在事务回调外生成，使驱动重试不会创建额外运行；暂停恢复
    沿用既有运行。传入租约时，每次事务回调都会先递增领取版本并确认 owner、fence
    和未到期时间，失效时抛出 ``SchedulerLeaseLost`` 让调度循环停止本周期。
    """
    resuming = task["status"] == "PAUSED" and bool(task.get("runId"))
    run_id = task["runId"] if resuming else new_id()
    cutoff = timedelta(seconds=15)

    async def commit(session):
        """在可重试事务回调中重新检查租约、节点、任务和运行锁。"""
        db = repo.db
        current_time = now()
        if lease is not None:
            held = await db.leaders.find_one_and_update(
                {
                    "_id": "scheduler",
                    "owner": lease["owner"],
                    "fence": lease["fence"],
                    "expires": {"$gt": current_time},
                },
                {"$inc": {"claimVersion": 1}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if held is None:
                raise SchedulerLeaseLost("调度租约在领取事务开始前已失效")

        node = await db.nodes.find_one_and_update(
            {
                "id": node_id,
                "heartbeat": {"$gte": current_time - cutoff},
                "diskPercent": {"$lt": 90},
                "accepting": True,
                "deletedAt": None,
            },
            {"$inc": {"claimVersion": 1}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if (
            node is None
            or node.get("isolated") or node.get("configurationMismatch")
            or resource_pressure(node, current_time)
        ):
            return None

        task_query = {
            "id": task["id"],
            "nodeId": None,
            "desiredState": "RUNNING",
            "resourceDeleted": {"$ne": True},
            "status": task["status"],
        }
        if resuming:
            task_query.update({
                "runId": run_id,
                "$or": [
                    {"resumeClaimToken": {"$exists": False}},
                    {"resumeClaimExpires": {"$exists": False}},
                    {"resumeClaimExpires": {"$lt": current_time}},
                ],
            })
        current = await db.tasks.find_one(task_query, session=session)
        if current is None:
            return None
        resource_ip = current.get("ip")
        # 旧迁移夹具和历史任务可能尚无资源绑定；正式创建路径始终具备 resourceId。
        if current.get("resourceId"):
            resource = await db.resources.find_one({"id": current["resourceId"], "deletedAt": None}, session=session)
            if resource is None or resource.get("healthStatus") in {"AUTH_FAILED", "OFFLINE", "ERROR"}:
                return None
            resource_ip = resource.get("ip")
            if requires_coredump_nfs(current, resource) and not (node.get("capabilities") or {}).get("coredumpNfs"):
                return None

        # 写入同一配置记录使并发管理员编辑与领取产生事务冲突，重试时复核新规则。
        node_config = await db.node_configs.find_one_and_update(
            {"id": node_id}, {"$inc": {"assignmentRevision": 1}},
            return_document=ReturnDocument.AFTER, session=session,
        )
        # 心跳中的容量和准入可能早于管理员保存；同一配置写栅栏保证竞争后重新复核。
        configured = node_config or {}
        capacity = configured.get("capacity", node.get("capacity", DEFAULT_NODE_CAPACITY))
        if not configured.get("accepting", True) or occupied >= capacity:
            return None
        if input_rate_blocked(node, node_config if node_config is not None else node):
            return None
        if write_latency_blocked(node, node_config if node_config is not None else node):
            return None
        if node_config and (node_config.get("deletedAt") or not accepts_resource(node_config, resource_ip)):
            return None

        lock = await db.endpoint_locks.find_one({"taskId": task["id"]}, session=session)
        if lock is not None and (not resuming or lock.get("runId") != run_id):
            await db.tasks.update_one(
                task_query,
                {"$set": {"status": "BLOCKED", "error": "同一任务已有活动运行锁"}},
                session=session,
            )
            return None
        if lock is None:
            await db.endpoint_locks.insert_one(
                {
                    "endpoint": f'{current["ip"]}:{current["port"]}',
                    "taskId": task["id"],
                    "runId": run_id,
                },
                session=session,
            )

        claim_update = {
            "$set": {"nodeId": node_id, "runId": run_id, "status": "PENDING", "error": None},
            "$inc": {"generation": 1},
        }
        if resuming:
            claim_update["$unset"] = {"resumeClaimToken": "", "resumeClaimExpires": ""}
        claimed = await db.tasks.find_one_and_update(
            task_query,
            claim_update,
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if claimed is None:
            raise _ClaimRejected("任务领取 CAS 未命中")
        if resuming:
            await db.endpoint_locks.update_one(
                {"taskId": task["id"], "runId": run_id},
                {"$unset": {"claimToken": ""}},
                session=session,
            )
        await db.runs.update_one(
            {"id": run_id},
            {
                "$set": {"nodeId": node_id, "generation": claimed["generation"]},
                "$setOnInsert": {"id": run_id, "taskId": task["id"], "startedAt": now()},
            },
            upsert=True,
            session=session,
        )
        return claimed

    try:
        return await claim_transaction(repo, commit)
    except _ClaimRejected:
        return None
