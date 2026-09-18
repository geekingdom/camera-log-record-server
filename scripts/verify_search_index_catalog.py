"""在真实 Mongo 随机库验证全文索引补建目录扫描使用复合索引。"""

import asyncio
import json
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


def stage_contains(value, stage):
    """兼容经典与 SBE Explain，递归识别索引扫描或阻塞排序阶段。"""
    if isinstance(value, dict):
        plan = value.get("queryPlan")
        return (value.get("stage") == stage or (isinstance(plan, dict) and plan.get("stage") == stage)
                or any(stage_contains(item, stage) for item in value.values()))
    return isinstance(value, list) and any(stage_contains(item, stage) for item in value)


def execution_stats(value):
    """提取 Explain 执行统计，服务端版本差异时继续向子文档查找。"""
    if isinstance(value, dict):
        if isinstance(value.get("executionStats"), dict):
            return value["executionStats"]
        for item in value.values():
            if result := execution_stats(item):
                return result
    if isinstance(value, list):
        for item in value:
            if result := execution_stats(item):
                return result
    return {}


async def verify():
    """创建、验证并删除随机库，全程不读取平台目录、设备或日志正文。"""
    configured = Settings()
    database_name = "search_index_catalog_verify_" + uuid4().hex
    client = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    removed = False
    try:
        database = client[database_name]
        settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=database_name,
                            encryption_key=Fernet.generate_key().decode(), start_background=False)
        await Repository(database, settings).initialize()
        rows = []
        for index in range(1000):
            node_id = "node-a" if index % 4 in (0, 1) else "node-b"
            status = "READY" if index % 3 else "OPEN"
            rows.append({"id": f"file-{index:04d}", "nodeId": node_id, "status": status})
        # 目标组合至少有 64 条，验证器的排序和上限与补建目录查询完全一致。
        await database.files.insert_many(rows)
        command = {"find": "files", "filter": {"nodeId": "node-a", "status": "READY"},
                   "sort": {"id": 1}, "limit": 64}
        explain = await database.command("explain", command, verbosity="executionStats")
        stats = execution_stats(explain)
        plan = explain.get("queryPlanner", {}).get("winningPlan", explain)
        returned = int(stats.get("nReturned", -1))
        keys, documents = int(stats.get("totalKeysExamined", -1)), int(stats.get("totalDocsExamined", -1))
        if not stage_contains(plan, "IXSCAN") or stage_contains(plan, "SORT"):
            raise AssertionError("目录补建查询未使用无排序索引计划")
        if returned != 64 or keys < returned or documents < returned or keys > returned + 4 or documents > returned + 4:
            raise AssertionError(f"目录补建扫描量异常 returned={returned} keys={keys} docs={documents}")
        return {"passed": True, "seededFiles": len(rows), "returned": returned,
                "keysExamined": keys, "docsExamined": documents, "noBlockingSort": True}
    finally:
        await client.drop_database(database_name)
        removed = database_name not in await client.list_database_names()
        await client.close()
        if not removed:
            raise AssertionError("全文索引目录随机数据库未被删除")


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False, sort_keys=True))
