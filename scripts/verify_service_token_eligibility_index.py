"""以真实 MongoDB 副本集验证当前身份服务账号资格查询的复合索引。"""

import asyncio
import json
from datetime import timedelta
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


def check(condition, message):
    """验证失败时给出明确索引或扫描量结论，不输出数据库连接凭据。"""
    if not condition:
        raise AssertionError(message)


def eligibility_query(user_id, instant):
    """与会话身份相同地匹配未撤销、无到期时间或尚未到期的服务账号。"""
    return {
        "userId": user_id,
        "revoked": False,
        "$or": [{"expiresAt": None}, {"expiresAt": {"$gt": instant}}],
    }


async def explain_find_one(database, query):
    """按正式 find_one 的 limit=1 取执行统计，并要求命中命名复合索引。"""
    explain = await database.command(
        "explain", {"find": "tokens", "filter": query, "projection": {"_id": 1}, "limit": 1},
        verbosity="executionStats",
    )
    stats = explain["executionStats"]
    plan = json.dumps(explain["queryPlanner"]["winningPlan"], default=str)
    check("tokens_active_user_expiry" in plan, "服务账号资格查询未命中 tokens_active_user_expiry 索引")
    return {
        "nReturned": int(stats["nReturned"]),
        "totalKeysExamined": int(stats["totalKeysExamined"]),
        "totalDocsExamined": int(stats["totalDocsExamined"]),
    }


async def main():
    """建立随机库、写入干扰令牌并在 finally 中删除数据库和临时日志目录。"""
    configured = Settings()
    database_name = "service_token_eligibility_" + uuid4().hex
    check(database_name != configured.database_name, "验证脚本不能使用配置中的业务数据库")
    logs = TemporaryDirectory(prefix="service-token-eligibility-")
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    initialized = False
    try:
        settings = configured.model_copy(update={
            "database_name": database_name,
            "encryption_key": Fernet.generate_key().decode(),
            "log_root": logs.name,
        })
        repo = Repository(mongo[database_name], settings)
        await repo.initialize()
        initialized = True
        instant = now()
        noise = [
            {"id": f"noise-{index}", "tokenHash": f"hash-{index}", "userId": f"other-{index % 20}",
             "revoked": index % 2 == 0, "expiresAt": instant + timedelta(days=1)}
            for index in range(400)
        ]
        await repo.db.tokens.insert_many(noise + [
            {"id": "active-future", "tokenHash": "active-future-hash", "userId": "has-active-future", "revoked": False,
             "expiresAt": instant + timedelta(days=1)},
            {"id": "active-null", "tokenHash": "active-null-hash", "userId": "has-active-null", "revoked": False,
             "expiresAt": None},
            {"id": "active-missing", "tokenHash": "active-missing-hash", "userId": "has-active-missing", "revoked": False},
            {"id": "expired", "tokenHash": "expired-hash", "userId": "no-active", "revoked": False,
             "expiresAt": instant - timedelta(days=1)},
            {"id": "revoked", "tokenHash": "revoked-hash", "userId": "no-active", "revoked": True,
             "expiresAt": instant + timedelta(days=1)},
        ])
        active_future = await explain_find_one(repo.db, eligibility_query("has-active-future", instant))
        active_null = await explain_find_one(repo.db, eligibility_query("has-active-null", instant))
        active_missing = await explain_find_one(repo.db, eligibility_query("has-active-missing", instant))
        absent = await explain_find_one(repo.db, eligibility_query("no-active", instant))
        for label, stats in {
            "future": active_future,
            "null": active_null,
            "missing": active_missing,
        }.items():
            check(stats["nReturned"] == 1 and stats["totalDocsExamined"] <= 1,
                  f"{label} 到期状态的有效服务账号资格查询扫描超出一条文档：{stats}")
        check(absent["nReturned"] == 0 and absent["totalDocsExamined"] == 0,
              f"无有效服务账号资格查询不应读取令牌文档：{absent}")
        result = {
            "passed": True,
            "realMongoReplicaSet": True,
            "active": {"future": active_future, "null": active_null, "missing": active_missing},
            "absent": absent,
        }
    finally:
        try:
            if initialized:
                await mongo.drop_database(database_name)
        finally:
            await mongo.close()
            logs.cleanup()
    print(json.dumps(result | {"temporaryDatabaseDropped": True, "temporaryLogDirectoryDropped": True}))


if __name__ == "__main__":
    asyncio.run(main())
