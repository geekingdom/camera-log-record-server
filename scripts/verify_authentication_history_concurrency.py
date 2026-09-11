"""在真实 MongoDB 副本集验证认证历史并发聚合和失败边界。

脚本只创建随机临时数据库，结束后无条件删除。MongoDB 单机不支持事务，因此会
先验证副本集能力并拒绝在不具备事务语义的目标上给出错误结论。
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.resources.authentication_records import record_authentication
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


async def verify(uri: str) -> dict[str, object]:
    """并发写入后断言成功聚合未跨越失败边界，最后清理临时数据库。"""
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000)
    # MongoDB 数据库名最多63字符；保持短前缀也便于人工识别并清理失败残留。
    database_name = f"cl_auth_{uuid.uuid4().hex}"
    try:
        hello = await client.admin.command("hello")
        if not hello.get("setName"):
            raise RuntimeError("认证历史并发验证需要 MongoDB 副本集事务")
        settings = Settings(
            _env_file=None, mongo_uri=uri, database_name=database_name,
            encryption_key=Fernet.generate_key().decode(), authentication_record_retention_days=90,
        )
        repo = Repository(client[database_name], settings)
        await repo.initialize()
        resource = {"id": "concurrent-camera", "authenticatedAt": datetime(2026, 9, 11, tzinfo=UTC),
                    "model": "DS-TEST", "subSerialNumber": "SERIAL-1"}
        started = datetime(2026, 9, 11, 8, tzinfo=UTC)
        # 首次 head 的并发 upsert 最容易遗漏唯一冲突重试；16 路都必须合为一个桶。
        await asyncio.gather(*(
            record_authentication(repo, resource, source="PERIODIC", result="SUCCESS", before=resource,
                                after=resource, completed_at=started)
            for _ in range(16)
        ))
        first_rows = [row async for row in repo.db.authentication_records.find({"resourceId": resource["id"]})]
        if [(row["result"], row.get("occurrenceCount")) for row in first_rows] != [("SUCCESS", 16)]:
            raise AssertionError("并发首次认证未聚合为单个同时间桶")
        # 失败与成功同批竞争，以事务代次检验边界，不假定 asyncio 的提交顺序。
        await asyncio.gather(
            record_authentication(repo, resource, source="PERIODIC", result="OFFLINE", before=resource,
                                  after=resource, completed_at=started + timedelta(minutes=1)),
            *(record_authentication(repo, resource, source="PERIODIC", result="SUCCESS", before=resource,
                                    after=resource, completed_at=started + timedelta(minutes=2, seconds=index))
              for index in range(16)),
        )
        rows = [row async for row in repo.db.authentication_records.find({"resourceId": resource["id"]}).sort("createdAt", 1)]
        failures = [row for row in rows if row["result"] == "OFFLINE"]
        successes = [row for row in rows if row["result"] == "SUCCESS"]
        actual = [(row["result"], row.get("occurrenceCount")) for row in rows]
        if len(failures) != 1 or sum(row.get("occurrenceCount", 1) for row in successes) != 32:
            raise AssertionError(f"认证历史并发计数丢失 actual={actual!r}")
        failure_revision = failures[0]["historyFirstRevision"]
        if any(row["historyFirstRevision"] < failure_revision < row["historyLatestRevision"] for row in successes):
            raise AssertionError(f"成功聚合跨越失败边界 actual={actual!r}")
        head = await repo.db.authentication_record_heads.find_one({"_id": resource["id"]})
        if not head or not any(row["id"] == head.get("latestRecordId") for row in rows):
            raise AssertionError("认证历史 head 未指向事务序列最后一条记录")
        return {"passed": True, "temporaryDatabase": database_name, "records": actual,
                "headRevision": head["revision"]}
    finally:
        await client.drop_database(database_name)
        await client.close()


if __name__ == "__main__":
    import os

    value = asyncio.run(verify(os.environ.get("MONGO_URI", Settings().mongo_uri)))
    print(value)
