"""用真实 MongoDB 事务验证保存配置与并发任务领取的准入边界。"""

import asyncio
import json
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.input_admission import MIB
from camera_logs.tasks.claim import claim_task
from pymongo import AsyncMongoClient
from verify_scheduler_transaction import DatabaseProxy, seed


async def assert_unclaimed(db, task):
    """拒绝领取后任务、运行和端点锁必须仍是同一未领取状态。"""
    stored = await db.tasks.find_one({"id": task["id"]})
    if stored is None or stored.get("nodeId") is not None:
        raise AssertionError("准入拒绝后任务仍保留节点归属")
    if await db.runs.count_documents({"taskId": task["id"]}):
        raise AssertionError("准入拒绝后留下孤立运行记录")
    if await db.endpoint_locks.count_documents({"taskId": task["id"]}):
        raise AssertionError("准入拒绝后留下孤立端点锁")


async def reject_after_config_barrier(repo, task, node, lease, initial, changed, *, occupied, label):
    """在领取配置写栅栏前提交管理员更新，事务重试后必须按新配置拒绝。"""
    db, triggered = repo.db, False
    await db.node_configs.insert_one({"id": node, **initial})

    async def update_config(collection, operation, args, kwargs):
        """仅拦截带事务 session 的配置写栅栏，避免种子或普通读取误触发。"""
        nonlocal triggered
        if operation == "before_find_one_and_update" and kwargs.get("session") is not None and not triggered:
            triggered = True
            await db.node_configs.update_one({"id": node}, {"$set": changed})

    repo.db = DatabaseProxy(db, {"node_configs": update_config})
    try:
        claimed = await claim_task(repo, task, node, lease=lease, occupied=occupied)
    finally:
        repo.db = db
    if not triggered:
        raise AssertionError(f"{label} 未触及配置写栅栏")
    if claimed is not None:
        raise AssertionError(f"{label} 的新保存配置仍允许领取")
    await assert_unclaimed(db, task)


async def verify_rate_conflict(repo):
    """原速率收紧竞争仍需在真实事务冲突重试后拒绝，并可在放宽后恢复。"""
    task, node, lease = await seed(repo, "input-limit")
    await repo.db.nodes.update_one({"id": node}, {"$set": {"inputBytesPerSecond": 2 * MIB}})
    await reject_after_config_barrier(
        repo, task, node, lease, {"inputRateLimitMiB": 3}, {"inputRateLimitMiB": 1},
        occupied=0, label="速率收紧",
    )
    await repo.db.node_configs.update_one({"id": node}, {"$set": {"inputRateLimitMiB": 3}})
    if not await claim_task(repo, task, node, lease=lease):
        raise AssertionError("速率配置放宽后任务未恢复领取")


async def verify_saved_admission_conflicts(repo):
    """关闭准入和缩小容量都必须覆盖未刷新心跳，并且不能留下部分领取。"""
    disabled, disabled_node, disabled_lease = await seed(repo, "saved-disabled")
    await reject_after_config_barrier(
        repo, disabled, disabled_node, disabled_lease, {"accepting": True, "capacity": 4},
        {"accepting": False}, occupied=0, label="关闭 accepting",
    )

    limited, limited_node, limited_lease = await seed(repo, "saved-capacity")
    await reject_after_config_barrier(
        repo, limited, limited_node, limited_lease, {"accepting": True, "capacity": 4},
        {"capacity": 1}, occupied=1, label="容量缩小",
    )


async def verify_capacity_raise_without_heartbeat(repo):
    """管理员放宽容量后不等待 Worker 刷新旧心跳，调度领取应立即采用保存值。"""
    task, node, lease = await seed(repo, "saved-raised")
    await repo.db.nodes.update_one({"id": node}, {"$set": {"capacity": 1}})
    before = await repo.db.nodes.find_one({"id": node})
    await repo.db.node_configs.insert_one({"id": node, "accepting": True, "capacity": 1})
    await repo.db.node_configs.update_one({"id": node}, {"$set": {"capacity": 2}})
    after = await repo.db.nodes.find_one({"id": node})
    if before is None or after is None or after["heartbeat"] != before["heartbeat"]:
        raise AssertionError("容量放宽验证意外依赖了新 Worker 心跳")
    if not await claim_task(repo, task, node, lease=lease, occupied=1):
        raise AssertionError("保存容量放宽后旧心跳任务未立即领取")


async def main():
    """仅使用随机数据库；每个管理员配置竞争都通过真实事务与写栅栏复核。"""
    settings = Settings()
    mongo = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    name = "input_admission_verify_" + uuid4().hex
    repo = Repository(mongo[name], settings)
    try:
        await repo.initialize()
        await verify_rate_conflict(repo)
        await verify_saved_admission_conflicts(repo)
        await verify_capacity_raise_without_heartbeat(repo)
        print(json.dumps({
            "rateConflictRejected": True,
            "savedAcceptingConflictRejected": True,
            "savedCapacityConflictRejected": True,
            "noPartialRunOrLock": True,
            "savedCapacityUsedBeforeHeartbeat": True,
        }))
    finally:
        await mongo.drop_database(name)
        await mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
