"""用真实Mongo事务验证管理员收紧速率配置与并发任务领取的冲突重试。"""

import asyncio
import json
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.input_admission import MIB
from camera_logs.tasks.claim import claim_task
from pymongo import AsyncMongoClient
from verify_scheduler_transaction import DatabaseProxy, seed


async def main():
    """仅使用随机数据库；配置并发更新后旧快照不得突破新准入上限。"""
    settings = Settings()
    mongo = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    name = "input_admission_verify_" + uuid4().hex
    repo = Repository(mongo[name], settings)
    try:
        await repo.initialize()
        task, node, lease = await seed(repo, "input-limit")
        await repo.db.nodes.update_one({"id": node}, {"$set": {"inputBytesPerSecond": 2 * MIB}})
        await repo.db.node_configs.insert_one({"id": node, "inputRateLimitMiB": 3})
        db, triggered = repo.db, False

        async def tighten(collection, operation, args, kwargs):
            """在领取已建立快照、配置写锁尚未取得时提交真实并发配置更新。"""
            nonlocal triggered
            if operation == "before_find_one_and_update" and not triggered:
                triggered = True
                await db.node_configs.update_one({"id": node}, {"$set": {"inputRateLimitMiB": 1}})

        repo.db = DatabaseProxy(db, {"node_configs": tighten})
        assert await claim_task(repo, task, node, lease=lease) is None
        repo.db = db
        assert triggered
        assert await db.runs.count_documents({"taskId": task["id"]}) == 0
        assert await db.endpoint_locks.count_documents({"taskId": task["id"]}) == 0
        await db.node_configs.update_one({"id": node}, {"$set": {"inputRateLimitMiB": 3}})
        assert await claim_task(repo, task, node, lease=lease)
        print(json.dumps({"configConflictRejected": True, "noPartialRunOrLock": True, "recovery": True}))
    finally:
        await mongo.drop_database(name)
        await mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
