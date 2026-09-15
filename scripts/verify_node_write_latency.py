"""在随机本机Mongo库中验证每节点写入延迟配置与领取事务竞争。"""

import asyncio
import json
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.tasks.claim import claim_task
from pymongo import AsyncMongoClient
from verify_cross_worker_ssh_resume import local_mongo_uri
from verify_input_admission import assert_unclaimed, reject_after_config_barrier
from verify_scheduler_transaction import seed


async def verify_config_conflict(repo):
    """领取快照后收紧阈值必须拒绝；放宽后用相同心跳即可按保存配置领取。"""
    task, node, lease = await seed(repo, "latency-conflict")
    await repo.db.nodes.update_one({"id": node}, {"$set": {"writeLatencyMs": 350}})
    await reject_after_config_barrier(
        repo, task, node, lease, {"writeLatencyLimitMs": 500}, {"writeLatencyLimitMs": 200},
        occupied=0, label="写入延迟阈值收紧",
    )
    await repo.db.node_configs.update_one({"id": node}, {"$set": {"writeLatencyLimitMs": 500}})
    if await claim_task(repo, task, node, lease=lease) is None:
        raise AssertionError("阈值放宽后仍被旧200ms限制拒绝")


async def verify_node_independence(repo):
    """相同延迟在缺省节点被拒绝、独立配置节点允许，不能把放宽传播到其它节点。"""
    first, first_node, first_lease = await seed(repo, "latency-default")
    await repo.db.nodes.update_one({"id": first_node}, {"$set": {"writeLatencyMs": 250}})
    if await claim_task(repo, first, first_node, lease=first_lease) is not None:
        raise AssertionError("未配置节点没有继续采用200ms默认阈值")
    await assert_unclaimed(repo.db, first)
    second, second_node, second_lease = await seed(repo, "latency-custom")
    await repo.db.nodes.update_one({"id": second_node}, {"$set": {"writeLatencyMs": 250}})
    await repo.db.node_configs.insert_one({"id": second_node, "writeLatencyLimitMs": 500})
    if await claim_task(repo, second, second_node, lease=second_lease) is None:
        raise AssertionError("单独放宽节点未按自身阈值接收任务")
    await assert_unclaimed(repo.db, first)


async def main():
    """只操作本机随机库，不连接设备，失败或成功都删除本次数据库。"""
    settings = Settings()
    if not local_mongo_uri(settings.mongo_uri):
        raise RuntimeError("仅允许本机Mongo进行隔离事务验证")
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    name = "node_latency_verify_" + uuid4().hex
    try:
        repo = Repository(client[name], settings)
        await repo.initialize()
        await verify_config_conflict(repo)
        await verify_node_independence(repo)
        print(json.dumps({"passed": True, "configurationConflictRejected": True,
                          "relaxedLimitClaimed": True, "nodeLimitsIndependent": True,
                          "noPartialRunOrLock": True}))
    finally:
        try:
            await client.drop_database(name)
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())
