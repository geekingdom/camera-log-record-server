"""在随机真实 MongoDB 副本集验证资源摘要与软删操作的高频查询索引。"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.resources.listing import _add_task_summaries
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from verify_isolated_browser_smoke import local_mongo_uri


async def explain(db, command: dict) -> dict:
    """返回执行统计并拒绝全表扫描或阻塞排序，验证实际索引而非 MongoMock 行为。"""
    result = await db.command("explain", command, verbosity="executionStats")
    stats = result["executionStats"]
    plan = json.dumps(result["queryPlanner"]["winningPlan"], default=str)
    if "IXSCAN" not in plan or '"stage": "SORT"' in plan:
        collection = command.get("aggregate", command.get("find", "未知集合"))
        raise AssertionError(f"{collection} 查询未命中预期索引 plan={plan}")
    def indexes(value):
        """遍历实际获胜计划，不能仅因其它索引命中就声称新增索引生效。"""
        if isinstance(value, dict):
            found = {value["indexName"]} if "indexName" in value else set()
            return found.union(*(indexes(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(indexes(child) for child in value))
        return set()
    return {**{key: stats[key] for key in ("nReturned", "totalKeysExamined", "totalDocsExamined")},
            "indexes": sorted(indexes(result["queryPlanner"]["winningPlan"]))}


class CapturingTaskAggregate:
    """转发真实 tasks.aggregate，并保留资源页实际传入的聚合管道。"""

    def __init__(self, tasks):
        self._tasks = tasks
        self.pipeline: list[dict] | None = None

    def aggregate(self, pipeline: list[dict]):
        """不改写生产管道，供汇总函数执行和后续 Explain 复用。"""
        self.pipeline = pipeline
        return self._tasks.aggregate(pipeline)


class CapturingDatabase:
    """只暴露资源摘要调用所需的 tasks 聚合集合。"""

    def __init__(self, tasks):
        self.tasks = CapturingTaskAggregate(tasks)


async def verify(uri: str) -> dict[str, object]:
    """写入随机元数据、验证扫描上界，finally 只删除本次随机数据库。"""
    if not local_mongo_uri(uri):
        raise RuntimeError("索引验收仅允许回环Mongo地址")
    database_name = f"database_hot_paths_verify_{uuid4().hex}"
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    connected = False
    try:
        await client.admin.command("hello")
        connected = True
        settings = Settings(_env_file=None, mongo_uri=uri, database_name=database_name,
                            encryption_key=Fernet.generate_key().decode(), start_background=False)
        repo = Repository(client[database_name], settings)
        await repo.initialize()
        stamp = now()
        resource_ids = [f"resource-{index}" for index in range(20)]
        await repo.db.resources.insert_many([{"id": identifier, "deletedAt": None, "createdAt": stamp}
                                              for identifier in resource_ids])
        tasks = []
        for resource_id in resource_ids:
            tasks.extend({"id": f"{resource_id}-active-{index}", "resourceId": resource_id,
                          "deletedAt": None, "status": "STOPPED", "desiredState": "STOPPED", "nodeId": None}
                         for index in range(100))
            tasks.extend({"id": f"{resource_id}-historical-{index}", "resourceId": resource_id,
                          "deletedAt": stamp, "status": "STOPPED", "desiredState": "STOPPED", "nodeId": None}
                         for index in range(200))
        # 一半任务从串口服务器分支关联；部分旧任务省略deletedAt，验证null兼容缺省。
        for index, task in enumerate(tasks):
            if index % 2:
                task["serialServerResourceId"] = task["resourceId"]
                task["resourceId"] = "another-device"
            if index % 3 == 0 and task.get("deletedAt") is None:
                task.pop("deletedAt")
        await repo.db.tasks.insert_many(tasks)
        selected = resource_ids[:5]
        summary_resources = [{"id": identifier} for identifier in selected]
        captured_database = CapturingDatabase(repo.db.tasks)
        await _add_task_summaries(captured_database, summary_resources)
        task_pipeline = captured_database.tasks.pipeline
        if task_pipeline is None:
            raise AssertionError("资源摘要未调用 tasks.aggregate")
        if any(resource["taskCount"] != 100 or resource["activeTaskCount"] != 0
               or resource["unsettledTaskCount"] != 0 for resource in summary_resources):
            raise AssertionError(f"资源摘要返回值不符合生产语义 resources={summary_resources}")

        await repo.db.operations.insert_many([
            {"id": f"pending-{index}", "taskId": "resource-0-active-0", "status": "PENDING",
             "desiredState": "RUNNING", "createdAt": stamp}
            for index in range(500)
        ] + [
            {"id": f"terminal-{index}", "taskId": "resource-0-active-0", "status": "SUCCEEDED",
             "desiredState": "RUNNING", "createdAt": stamp}
            for index in range(500)
        ])
        operation_query = {
            "taskId": "resource-0-active-0", "status": "PENDING", "desiredState": {"$ne": "STOPPED"},
        }
        # 只在随机库移除本轮新索引，测得升级前后扫描量；重复初始化验证幂等兼容。
        await repo.db.tasks.drop_index("tasks_resource_visible_summary")
        await repo.db.tasks.drop_index("tasks_serial_resource_visible_summary")
        await repo.db.operations.drop_index("operations_task_pending_intent")
        before_tasks = await explain(repo.db, {
            "aggregate": "tasks", "pipeline": task_pipeline, "cursor": {},
        })
        before_operations = await explain(repo.db, {
            "find": "operations", "filter": operation_query,
        })
        await repo.initialize()
        await repo.initialize()
        task_summary = await explain(repo.db, {
            "aggregate": "tasks", "pipeline": task_pipeline, "cursor": {},
        })
        operation_cancel = await explain(repo.db, {
            "find": "operations", "filter": operation_query,
        })
        if not {"tasks_resource_visible_summary", "tasks_serial_resource_visible_summary"}.issubset(task_summary["indexes"]):
            raise AssertionError(f"资源摘要两个关联分支未使用新索引 stats={task_summary}")
        if "operations_task_pending_intent" not in operation_cancel["indexes"]:
            raise AssertionError(f"操作取消未使用新部分索引 stats={operation_cancel}")
        # 两个分支各有有限的索引定位开销；键扫描仍须与当前页500条可见任务同量级，
        # 不能退化为读取20个资源的全部2,000条可见任务。
        if (task_summary["nReturned"] != 500 or task_summary["totalDocsExamined"] > 500
                or task_summary["totalKeysExamined"] > 510):
            raise AssertionError(f"资源摘要扫描超过当前页任务数 stats={task_summary}")
        if operation_cancel["nReturned"] != 500 or operation_cancel["totalDocsExamined"] > 500:
            raise AssertionError(f"软删取消操作扫描超过待处理操作数 stats={operation_cancel}")
        return {"passed": True, "temporaryDatabase": database_name, "taskSummary": task_summary,
                "operationCancel": operation_cancel, "beforeTaskSummary": before_tasks,
                "beforeOperationCancel": before_operations, "repeatedInitializationPassed": True}
    finally:
        try:
            if connected:
                await client.drop_database(database_name)
        finally:
            await client.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify(Settings().mongo_uri)), ensure_ascii=False, sort_keys=True))
