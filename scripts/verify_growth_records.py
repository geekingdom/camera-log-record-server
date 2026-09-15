"""真实Mongo副本集验证归档分段、删除/摘要回滚和维护查询Explain，结束删除随机库。"""

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

from camera_logs.common import record_archive
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from pymongo import AsyncMongoClient


class CollectionProxy:
    """在摘要增量写入后抛错，使生产事务回滚已经发生的删除。"""

    def __init__(self, collection):
        self.collection = collection

    async def update_one(self, *args, **kwargs):
        result = await self.collection.update_one(*args, **kwargs)
        if "$inc" in args[1]:
            raise RuntimeError("INJECTED_SUMMARY_FAILURE")
        return result

    def __getattr__(self, name):
        return getattr(self.collection, name)


class DatabaseProxy:
    """只代理指定摘要集合，保留真实PyMongo客户端和事务。"""

    def __init__(self, db, target):
        self.db, self.target, self.client = db, target, db.client

    def __getattr__(self, name):
        return CollectionProxy(self.db[name]) if name == self.target else getattr(self.db, name)

    def __getitem__(self, name):
        return getattr(self, name)


async def main():
    """不连接设备；验证真实数据提交语义，Explain检查扫描量与阻塞排序。"""
    settings, name = Settings(), "growth_verify_" + uuid4().hex
    mongo = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    db = mongo[name]
    repo = Repository(db, settings)
    try:
        await repo.initialize()
        stamp = now() - timedelta(days=100)
        await db.runs.insert_one({"id": "run", "taskId": "task", "startedAt": stamp, "endedAt": stamp})
        await db.commands.insert_many([{"id": f"cmd-{i:05}", "taskId": "task", "runId": "run",
            "status": "SENT", "createdAt": stamp} for i in range(1001)])
        await db.audit.insert_one({"id": "audit", "createdAt": stamp})
        # 两种摘要故障均发生在删除之后；事务必须恢复正文且不保留累计计数。
        for target, config, source in (("run_archives", {"runDays": 30}, "commands"),
                                       ("record_purge_days", {"auditDays": 30}, "audit")):
            original = await db[source].count_documents({})
            repo.db = DatabaseProxy(db, target)
            try:
                await record_archive.maintain_growth_records(repo, config)
            except RuntimeError as error:
                assert str(error) == "INJECTED_SUMMARY_FAILURE"
            else:
                raise AssertionError("故障注入未命中")
            finally:
                repo.db = db
            assert await db[source].count_documents({}) == original
            assert await db[target].count_documents({}) == 0
        first = await record_archive.maintain_growth_records(repo, {"runDays": 30})
        assert first["partialRuns"] == 1
        assert await db.commands.count_documents({}) == 901
        for _ in range(10):
            await record_archive.maintain_growth_records(repo, {"runDays": 30})
        summary = await db.run_archives.find_one({"runId": "run"})
        assert summary["state"] == "ARCHIVED" and summary["commandCount"] == 1001
        assert await db.runs.count_documents({}) == 0
        # 显式活租约拒绝另一维护者，过期后恢复；不依靠MongoMock声明原子性。
        await db.record_archive_maintenance.update_one({"_id": "growth-records"},
            {"$set": {"owner": "other", "expiresAt": now() + timedelta(minutes=1)}})
        assert not (await record_archive.maintain_growth_records(repo, {"auditDays": 30}))["leased"]
        await db.record_archive_maintenance.update_one({"_id": "growth-records"}, {"$set": {"expiresAt": stamp}})
        assert (await record_archive.maintain_growth_records(repo, {"auditDays": 30}))["audit"] == 1
        explains = {}
        cases = [("runs", {"endedAt": {"$lt": now()}}, {"endedAt": 1, "id": 1}),
                 ("commands", {"taskId": "task", "runId": "run"}, {"id": 1}),
                 ("idempotency", {"resourceId": "cmd", "expiresAt": {"$gt": now()}}, {}),
                 ("operations", {"createdAt": {"$lt": now()}}, {"createdAt": 1, "_id": 1}),
                 ("jobs", {"nodeId": "node", "kind": "DOWNLOAD", "status": {"$in": ["SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"]},
                           "outputExecutionState": {"$in": ["CLOSED", "WRITING"]}, "expiresAt": {"$lte": now()}}, {"expiresAt": 1, "id": 1})]
        for collection, query, order in cases:
            await db[collection].insert_many([{"id": f"filler-{i}", "taskId": "task", "runId": "run",
                "endedAt": stamp, "createdAt": stamp, "actor": "verify", "key": str(i),
                "nodeId": "node", "kind": "DOWNLOAD", "status": "FAILED", "outputExecutionState": "CLOSED",
                "resourceId": "cmd", "expiresAt": stamp if collection == "jobs" else now()+timedelta(days=1)} for i in range(500)])
            command = {"find": collection, "filter": query, "limit": 10}
            if order:
                command["sort"] = order
            explain = await db.command("explain", command, verbosity="executionStats")
            stats = explain["executionStats"]
            plan = json.dumps(explain["queryPlanner"]["winningPlan"], default=str)
            assert '"stage": "SORT"' not in plan and "IXSCAN" in plan, plan
            assert stats["totalDocsExamined"] <= 10
            explains[collection] = {key: stats[key] for key in ("nReturned", "totalKeysExamined", "totalDocsExamined")}
        print(json.dumps({"rollback": True, "segmented1001": True, "leaseExclusion": True, "explain": explains}))
    finally:
        await mongo.drop_database(name)
        await mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
