"""根据节点容量分配采集任务，使用端点锁阻止重复连接。

心跳超时只能证明节点失联，不能证明旧设备连接已释放。因此进入阻塞状态，
必须确认旧实例停止或完成外部隔离后才能接管；SSH 继续采集保留原运行和预算。
"""
import asyncio
import logging
from datetime import timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.resources.lifecycle import reconcile_deleted_resources

logger = logging.getLogger(__name__)


async def schedule_once(repo):
    """在一次持有调度租约的周期内处理停止、失联和待分配任务。"""
    db = repo.db
    await reconcile_deleted_resources(repo)
    cutoff = now() - timedelta(seconds=30)
    async for node in db.nodes.find({"heartbeat": {"$lt": cutoff}}):
        await db.tasks.update_many({"nodeId": node["id"], "status": {"$nin": ["STOPPED", "BLOCKED"]}},
            {"$set": {"status": "BLOCKED", "error": "节点失联，必须确认旧实例停止或隔离后才能接管"}})
    async for task in db.tasks.find({"desiredState": "STOPPED", "nodeId": None}):
        if task["status"] not in ("BLOCKED", "ERROR"):
            await db.tasks.update_one({"id": task["id"], "nodeId": None}, {"$set": {"status": "STOPPED"}})
            await db.operations.update_many({"taskId": task["id"], "desiredState": "STOPPED", "status": "PENDING"},
                                            {"$set": {"status": "SUCCEEDED", "completedAt": now()}})
    active = await db.tasks.count_documents({"nodeId": {"$ne": None}})
    if active >= repo.settings.cluster_capacity:
        return
    async for task in db.tasks.find({"desiredState": "RUNNING", "nodeId": None,
                                    "resourceDeleted": {"$ne": True},
                                    "status": {"$in": ["STOPPED", "PENDING", "PAUSED"]}}).limit(500):
        nodes = [n async for n in db.nodes.find({"heartbeat": {"$gte": now()-timedelta(seconds=15)},
                                                "diskPercent": {"$lt": 90}, "accepting": True})]
        candidates = []
        for node in nodes:
            count = await db.tasks.count_documents({"nodeId": node["id"]})
            if count < node.get("capacity", 100) and node.get("writeLatencyMs", 0) <= 200:
                candidates.append((count, node.get("inputBytesPerSecond", 0), node["id"]))
        if not candidates:
            if task["status"] != "PAUSED":
                await db.tasks.update_one({"id": task["id"], "nodeId": None}, {"$set": {"status": "PENDING"}})
            continue
        node_id = min(candidates)[2]
        resuming = task["status"] == "PAUSED" and bool(task.get("runId"))
        run_id = task["runId"] if resuming else new_id()
        endpoint = f'{task["ip"]}:{task["port"]}'
        claim_token = new_id() if resuming else None
        claim_expires = now() + timedelta(seconds=10) if resuming else None
        if resuming:
            # 同一暂停运行只允许一个短期恢复租约，崩溃遗留的 token 到期后可被新周期接管。
            reservation = await db.tasks.find_one_and_update(
                {"id": task["id"], "runId": run_id, "nodeId": None, "status": "PAUSED",
                 "desiredState": "RUNNING", "$or": [
                     {"resumeClaimToken": {"$exists": False}},
                     {"resumeClaimExpires": {"$exists": False}},
                     {"resumeClaimExpires": {"$lt": now()}},
                 ]},
                {"$set": {"resumeClaimToken": claim_token, "resumeClaimExpires": claim_expires}},
                return_document=ReturnDocument.AFTER,
            )
            if not reservation:
                continue
        try:
            if resuming:
                await db.endpoint_locks.find_one_and_update(
                    {"endpoint": endpoint, "taskId": task["id"], "runId": run_id},
                    {"$set": {"claimToken": claim_token},
                     "$setOnInsert": {"endpoint": endpoint, "taskId": task["id"], "runId": run_id}},
                    upsert=True, return_document=ReturnDocument.AFTER,
                )
            else:
                await db.endpoint_locks.insert_one({"endpoint": endpoint, "taskId": task["id"], "runId": run_id})
        except DuplicateKeyError:
            if resuming:
                await db.tasks.update_one(
                    {"id": task["id"], "runId": run_id, "nodeId": None, "resumeClaimToken": claim_token},
                    {"$unset": {"resumeClaimToken": "", "resumeClaimExpires": ""}},
                )
            await db.tasks.update_one({"id": task["id"], "nodeId": None},
                {"$set": {"status": "BLOCKED", "error": "同一任务已有活动运行锁"}})
            continue
        if resuming:
            owned_lock = await db.endpoint_locks.find_one(
                {"endpoint": endpoint, "taskId": task["id"], "runId": run_id, "claimToken": claim_token}
            )
            if not owned_lock:
                await db.tasks.update_one(
                    {"id": task["id"], "runId": run_id, "nodeId": None, "resumeClaimToken": claim_token},
                    {"$unset": {"resumeClaimToken": "", "resumeClaimExpires": ""}},
                )
                continue
        claim_query = {"id": task["id"], "nodeId": None, "desiredState": "RUNNING",
                       "resourceDeleted": {"$ne": True}}
        if resuming:
            claim_query.update({
                "runId": run_id, "status": "PAUSED", "resumeClaimToken": claim_token,
                "resumeClaimExpires": {"$gte": now()},
            })
        claim_update = {
            "$set": {"nodeId": node_id, "runId": run_id, "status": "PENDING", "error": None},
            "$inc": {"generation": 1},
        }
        if resuming:
            claim_update["$unset"] = {"resumeClaimToken": "", "resumeClaimExpires": ""}
        claimed = await db.tasks.find_one_and_update(
            claim_query, claim_update, return_document=ReturnDocument.AFTER
        )
        if claimed:
            if resuming:
                await db.endpoint_locks.update_one(
                    {"endpoint": endpoint, "taskId": task["id"], "runId": run_id, "claimToken": claim_token},
                    {"$unset": {"claimToken": ""}},
                )
            await db.runs.update_one({"id": run_id}, {"$set": {"nodeId": node_id, "generation": claimed["generation"]},
                "$setOnInsert": {"id": run_id, "taskId": task["id"], "startedAt": now()}}, upsert=True)
            active += 1
        elif not resuming:
            await db.endpoint_locks.delete_one({"runId": run_id})
        else:
            # 仅本次 token 能回收恢复锁；后继领取写入的新 token 不受旧补偿影响。
            stopped = await db.tasks.find_one({
                "id": task["id"], "runId": run_id, "nodeId": None, "desiredState": "STOPPED",
                "resumeClaimToken": claim_token,
            })
            if stopped:
                await db.endpoint_locks.delete_one(
                    {"endpoint": endpoint, "taskId": task["id"], "runId": run_id, "claimToken": claim_token}
                )
            await db.tasks.update_one(
                {"id": task["id"], "runId": run_id, "nodeId": None, "resumeClaimToken": claim_token},
                {"$unset": {"resumeClaimToken": "", "resumeClaimExpires": ""}},
            )
        if active >= repo.settings.cluster_capacity:
            break


async def scheduler_loop(repo):
    """通过 MongoDB 原子租约选出调度者，限制周期耗时并记录异常。"""
    owner = new_id()
    while True:
        try:
            await repo.db.leaders.update_one({"_id": "scheduler"}, {"$setOnInsert": {"owner": "", "expires": now()}}, upsert=True)
            lock = await repo.db.leaders.find_one_and_update({"_id": "scheduler", "$or": [
                {"owner": owner}, {"expires": {"$lt": now()}}]},
                {"$set": {"owner": owner, "expires": now()+timedelta(seconds=10)}}, return_document=ReturnDocument.AFTER)
            if lock:
                await asyncio.wait_for(schedule_once(repo), timeout=8)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("调度周期失败")
        await asyncio.sleep(2)
