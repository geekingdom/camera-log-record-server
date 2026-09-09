"""根据节点容量分配采集任务，使用端点锁阻止重复连接。

心跳超时只能证明节点失联，不能证明旧设备连接已释放。因此进入阻塞状态，
必须确认旧实例停止或完成外部隔离后才能接管；SSH 继续采集保留原运行和预算。
"""
import asyncio
import logging
from datetime import timedelta

from pymongo import ReturnDocument

from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.resources.lifecycle import reconcile_deleted_resources
from camera_logs.tasks.claim import SchedulerLeaseLost, claim_task

logger = logging.getLogger(__name__)


async def reconcile_stopped_tasks(db):
    """批量收尾无归属任务，避免已完成的历史记录逐项占用调度租约。

    写入条件重新检查停止意图，不能覆盖并发启动。仅从待完成的停止操作
    关联任务，过滤异常及仍有节点的任务后限量处理，避免其阻塞后续操作。
    """
    await db.tasks.update_many(
        {"desiredState": "STOPPED", "nodeId": None,
         "status": {"$nin": ["STOPPED", "BLOCKED", "ERROR"]}},
        {"$set": {"status": "STOPPED"}},
    )
    cursor = await db.operations.aggregate([
        {"$match": {"desiredState": "STOPPED", "status": "PENDING"}},
        {"$lookup": {"from": "tasks", "localField": "taskId", "foreignField": "id", "as": "task"}},
        {"$unwind": "$task"},
        {"$match": {"task.desiredState": "STOPPED", "task.nodeId": None, "task.status": "STOPPED"}},
        {"$limit": 500},
        {"$project": {"id": 1, "_id": 0}},
    ])
    identifiers = [item["id"] async for item in cursor]
    if identifiers:
        # 启动 API 会先取消相反意图的待完成操作；不能把已取消操作改为成功。
        await db.operations.update_many(
            {"id": {"$in": identifiers}, "desiredState": "STOPPED", "status": "PENDING"},
            {"$set": {"status": "SUCCEEDED", "completedAt": now()}},
        )


async def schedule_once(repo, lease=None):
    """在一次持有调度租约的周期内处理停止、失联和待分配任务。"""
    db = repo.db
    await reconcile_deleted_resources(repo)
    cutoff = now() - timedelta(seconds=30)
    async for node in db.nodes.find({"heartbeat": {"$lt": cutoff}}):
        await db.tasks.update_many({"nodeId": node["id"], "status": {"$nin": ["STOPPED", "BLOCKED"]}},
            {"$set": {"status": "BLOCKED", "error": "节点失联，必须确认旧实例停止或隔离后才能接管"}})
    await reconcile_stopped_tasks(db)
    # 调度租约内只有本调度者新增归属，节点收尾只会释放归属。一次投影扫描统计
    # 全部占用（包括失联节点），本周期成功领取后递增；并发释放的容量下周期再用。
    # 只读取 nodeId，避免为每个任务的每个候选节点重复 count_documents。
    occupancy = {}
    async for assigned in db.tasks.find({"nodeId": {"$ne": None}}, {"nodeId": 1, "_id": 0}):
        owner = assigned["nodeId"]
        occupancy[owner] = occupancy.get(owner, 0) + 1
    active = sum(occupancy.values())
    if active >= repo.settings.cluster_capacity:
        return
    async for task in db.tasks.find({"desiredState": "RUNNING", "nodeId": None,
                                    "resourceDeleted": {"$ne": True},
                                    "status": {"$in": ["STOPPED", "PENDING", "PAUSED"]}}).limit(500):
        nodes = [n async for n in db.nodes.find({"heartbeat": {"$gte": now()-timedelta(seconds=15)},
                                                "diskPercent": {"$lt": 90}, "accepting": True, "deletedAt": None})]
        candidates = []
        for node in nodes:
            count = occupancy.get(node["id"], 0)
            if count < node.get("capacity", 100) and node.get("writeLatencyMs", 0) <= 200:
                candidates.append((count, node.get("inputBytesPerSecond", 0), node["id"]))
        if not candidates:
            if task["status"] != "PAUSED":
                # 候选查询期间用户可能已暂停或停止；旧快照只能回写同一排队意图。
                await db.tasks.update_one(
                    {"id": task["id"], "nodeId": None, "desiredState": "RUNNING",
                     "status": task["status"], "runId": task.get("runId"),
                     "generation": task.get("generation"), "resourceDeleted": {"$ne": True}},
                    {"$set": {"status": "PENDING"}},
                )
            continue
        node_id = min(candidates)[2]
        # 占用仅在领取事务确认提交后推进；未知提交会中止周期，下周期重读持久归属。
        claimed = await claim_task(repo, task, node_id, lease=lease, occupied=occupancy.get(node_id, 0))
        if claimed:
            occupancy[node_id] = occupancy.get(node_id, 0) + 1
            active += 1
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
                {"$set": {"owner": owner, "expires": now()+timedelta(seconds=10)},
                 "$inc": {"fence": 1}}, return_document=ReturnDocument.AFTER)
            if lock:
                await asyncio.wait_for(schedule_once(repo, lease={"owner": owner, "fence": lock["fence"]}), timeout=8)
        except asyncio.CancelledError:
            raise
        except SchedulerLeaseLost:
            logger.warning("调度租约已失效，结束本周期领取")
        except Exception:
            logger.exception("调度周期失败")
        await asyncio.sleep(2)
