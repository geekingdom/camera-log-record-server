"""在隔离 MongoDB 副本集验证常用命令的索引、审计和版本并发语义。"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import uuid4

from camera_logs.common.audited_mutations import audited_create, audited_mutation
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pymongo import AsyncMongoClient, ReturnDocument


def check(condition: bool, message: str) -> None:
    """用中文断言报告验证失败点，避免把不完整的验收误报为成功。"""
    if not condition:
        raise AssertionError(message)


def contains_index(plan: object, name: str) -> bool:
    """递归读取经典与 SBE 查询计划中的索引名称。"""
    if isinstance(plan, dict):
        return plan.get("indexName") == name or any(contains_index(value, name) for value in plan.values())
    return isinstance(plan, list) and any(contains_index(value, name) for value in plan)


def contains_stage(plan: object, stage: str) -> bool:
    """识别计划中的扫描和阻塞排序阶段，不依赖具体 Mongo 版本的树形结构。"""
    if isinstance(plan, dict):
        query_plan = plan.get("queryPlan")
        return (plan.get("stage") == stage or (stage == "SORT" and "$sort" in plan)
                or (isinstance(query_plan, dict) and query_plan.get("stage") == stage)
                or any(contains_stage(value, stage) for value in plan.values()))
    return isinstance(plan, list) and any(contains_stage(value, stage) for value in plan)


def execution_stats(plan: object) -> dict:
    """抽取 Explain 的返回、键扫描和文档扫描计数，报告不包含夹具正文。"""
    if isinstance(plan, dict):
        if isinstance(plan.get("executionStats"), dict):
            return plan["executionStats"]
        for value in plan.values():
            result = execution_stats(value)
            if result:
                return result
    if isinstance(plan, list):
        for value in plan:
            result = execution_stats(value)
            if result:
                return result
    return {}


async def explain_default_list(db, query: dict, index_name: str) -> dict:
    """验证实际默认列表的筛选和稳定排序命中专用索引且没有阻塞排序。"""
    plan = await db.command("explain", {
        "find": "common_commands", "filter": query,
        "sort": {"command": 1, "id": 1}, "limit": 100,
    }, verbosity="executionStats")
    winning = plan.get("queryPlanner", {}).get("winningPlan") or plan
    stats = execution_stats(plan)
    check(contains_index(winning, index_name), f"默认列表未命中{index_name}")
    check(not contains_stage(winning, "SORT"), "默认列表出现阻塞排序")
    return {
        "index": index_name,
        "nReturned": stats.get("nReturned", 0),
        "keysExamined": stats.get("totalKeysExamined", 0),
        "docsExamined": stats.get("totalDocsExamined", 0),
    }


async def verify(database_name: str, mongo_uri: str) -> dict:
    """对随机数据库执行真实事务和 Explain，并由调用方在 finally 删除全部夹具。"""
    settings = Settings(mongo_uri=mongo_uri, database_name=database_name,
                        encryption_key=Fernet.generate_key().decode(), start_background=False)
    client = AsyncMongoClient(mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    database = client[database_name]
    try:
        repository = Repository(database, settings)
        await repository.initialize()
        payload = {"command": "ifconfig", "description": "查看网络地址", "enabled": True}

        async def prepare(identifier: str) -> dict:
            stamp = now()
            return payload | {"id": identifier, "version": 1, "createdAt": stamp, "updatedAt": stamp}

        first = await audited_create(repository, "verify-admin", "common-command-create", "create_common_command",
                                     payload, "common_commands", prepare)
        replay = await audited_create(repository, "verify-admin", "common-command-create", "create_common_command",
                                      payload, "common_commands", prepare)
        check(first["id"] == replay["id"], "同幂等键重试没有返回原常用命令")
        check(await database.audit.count_documents({"action": "create_common_command", "targetId": first["id"]}) == 1,
              "同幂等键重试重复写入创建审计")

        async def update(description: str) -> dict:
            async def commit(session):
                result = await database.common_commands.find_one_and_update(
                    {"id": first["id"], "version": 1},
                    {"$set": {"description": description, "updatedAt": now()}, "$inc": {"version": 1}},
                    return_document=ReturnDocument.AFTER, session=session,
                )
                if result is None:
                    raise HTTPException(409, "常用命令版本已变化，请刷新")
                return result
            return await audited_mutation(repository, "verify-admin", "update_common_command", first["id"], commit)

        outcomes = await asyncio.gather(update("并发编辑A"), update("并发编辑B"), return_exceptions=True)
        successes = [result for result in outcomes if isinstance(result, dict)]
        conflicts = [result for result in outcomes if isinstance(result, HTTPException) and result.status_code == 409]
        check(len(successes) == 1 and successes[0]["version"] == 2 and len(conflicts) == 1,
              "并发版本编辑没有严格表现为一次成功和一次409")
        check(await database.audit.count_documents({"action": "update_common_command", "targetId": first["id"]}) == 1,
              "并发版本编辑产生了错误数量的更新审计")

        fixtures = [{"id": f"fixture-{number:04d}", "command": f"show item {number:04d}",
                     "description": "Explain夹具", "enabled": number < 1000, "version": 1,
                     "createdAt": now(), "updatedAt": now()} for number in range(1200)]
        await database.common_commands.insert_many(fixtures)
        return {
            "passed": True,
            "idempotentCreate": True,
            "transactionalAudit": True,
            "concurrentVersionCas": True,
            "plans": {
                "userEnabledDefault": await explain_default_list(database, {"enabled": True}, "common_commands_enabled_command"),
                "adminDefault": await explain_default_list(database, {}, "common_commands_command"),
            },
        }
    finally:
        await client.drop_database(database_name)
        deleted = database_name not in await client.list_database_names()
        await client.close()
        check(deleted, "常用命令验证临时数据库未删除")


def arguments() -> argparse.Namespace:
    """要求显式传入隔离副本集地址，避免脚本意外操作默认业务数据库。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-uri", required=True, help="仅可传入隔离 MongoDB 副本集 URI")
    return parser.parse_args()


if __name__ == "__main__":
    options = arguments()
    temporary_database = f"common_commands_verify_{uuid4().hex}"
    result = asyncio.run(verify(temporary_database, options.mongo_uri))
    print(json.dumps(result | {"temporaryDatabaseDropped": True}, ensure_ascii=False, sort_keys=True))
