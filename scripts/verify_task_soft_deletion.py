"""用真实 MongoDB 副本集验证任务软删除的事务、索引和历史制品边界。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from camera_logs.common.database import now
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError


def check(condition, message):
    """用稳定中文错误说明专项验证的不变量，不输出连接字符串或令牌。"""
    if not condition:
        raise AssertionError(message)


def contains_index(plan, name):
    """兼容 Classic 与 SBE Explain，将索引命中作为结构化输出的最低要求。"""
    return name in json.dumps(plan, default=str)


async def seed(db, repo, name, *, collecting=False):
    """写入不触发设备访问的最小资源、任务、操作和保留下载制品。"""
    resource_id, task_id = f"{name}-resource", f"{name}-task"
    await db.resources.insert_one({"id": resource_id, "version": 1, "name": name,
                                   "kind": "HIKVISION_NETWORK", "ip": "192.0.2.70", "deletedAt": None})
    await db.tasks.insert_one({"id": task_id, "version": 1, "name": name, "ip": "192.0.2.70",
                               "resourceId": resource_id, "serialServerResourceId": None,
                               "createdBy": "bootstrap", "desiredState": "RUNNING" if collecting else "STOPPED",
                               "status": "COLLECTING" if collecting else "STOPPED",
                               "nodeId": "verify-node" if collecting else None, "generation": 1,
                               "resourceDeleted": False, "createdAt": now(), "updatedAt": now()})
    await db.operations.insert_one({"id": f"{name}-operation", "taskId": task_id,
                                    "desiredState": "RUNNING", "status": "PENDING"})
    await db.jobs.insert_one({"id": f"{name}-download", "taskId": task_id, "kind": "DOWNLOAD",
                              "status": "SUCCEEDED", "resultPath": "/retained/artifact.tar.gz", "expiresAt": now()})
    return resource_id, task_id


async def main():
    """随机库覆盖真实 Explain、事务回滚、级联和删除竞争，最后总会回收。"""
    configured, name, token = Settings(), "task_soft_delete_" + uuid4().hex, uuid4().hex
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                             w="majority", journal=True)
    temporary = TemporaryDirectory(prefix="task-soft-delete-")
    try:
        settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=name,
                            encryption_key=Fernet.generate_key().decode(), bootstrap_token=token,
                            admin_password="", start_background=False, log_root=Path(temporary.name) / "logs")
        app, db = create_app(settings, mongo[name]), mongo[name]
        async with app.router.lifespan_context(app):
            repo = app.state.repo
            for index in range(120):
                await db.tasks.insert_one({"id": f"visible-{index}", "name": f"可见任务 {index}", "deletedAt": None,
                                           "desiredState": "RUNNING", "nodeId": None, "status": "STOPPED",
                                           "createdAt": now()})
            await db.tasks.insert_many([{"id": f"deleted-{index}", "name": f"已删除任务 {index}", "deletedAt": now(), "deletionState": "DONE",
                                         "desiredState": "STOPPED", "nodeId": None, "status": "STOPPED",
                                         "createdAt": now()} for index in range(120)])
            created = await db.command("explain", {"find": "tasks", "filter": {"deletedAt": None},
                                       "sort": {"createdAt": -1, "id": 1}}, verbosity="executionStats")
            scheduled = await db.command("explain", {"find": "tasks", "filter": {
                "deletedAt": None, "desiredState": "RUNNING", "nodeId": None,
                "status": {"$in": ["STOPPED", "PENDING", "PAUSED"]}}}, verbosity="executionStats")
            cleanup = await db.command("explain", {"find": "tasks", "filter": {
                "deletedAt": {"$ne": None}, "deletionState": "PENDING"}}, verbosity="executionStats")
            check(contains_index(created, "tasks_visible_created"), "默认任务列表未命中可见列表索引")
            check(contains_index(scheduled, "tasks_visible_scheduler"), "调度候选未命中可见调度索引")
            check(contains_index(cleanup, "tasks_deleted_reconcile"), "删除收尾未命中专用索引")

            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(transport=transport, base_url="http://verify") as client:
                resource_id, task_id = await seed(db, repo, "task")
                deleted = await client.delete(f"/api/v1/tasks/{task_id}?version=1", headers=headers)
                check(deleted.status_code == 202, "任务删除未受理")
                task = await db.tasks.find_one({"id": task_id})
                check(task.get("deletedAt") and task["desiredState"] == "STOPPED", "任务删除标记不完整")
                check(await db.audit.find_one({"action": "delete_task", "targetId": task_id}) is not None,
                      "任务删除缺少同事务审计")
                check(await db.jobs.find_one({"id": "task-download", "resultPath": "/retained/artifact.tar.gz"}) is not None,
                      "任务删除错误删除下载制品")
                check((await client.get("/api/v1/tasks", headers=headers)).json()["total"] == 120,
                      "默认列表泄露已删除任务")
                check((await client.get("/api/v1/tasks?includeDeleted=true", headers=headers)).json()["total"] >= 241,
                      "includeDeleted 未返回软删除任务")

                stats_resource = "stats-resource"
                await db.resources.insert_one({"id": stats_resource, "version": 1, "name": "统计资源",
                                               "kind": "HIKVISION_NETWORK", "ip": "192.0.2.71", "deletedAt": None})
                await db.tasks.insert_many([
                    {"id": "stats-visible", "name": "统计可见任务", "resourceId": stats_resource, "deletedAt": None, "status": "STOPPED", "desiredState": "STOPPED"},
                    {"id": "stats-deleted", "name": "统计已删除任务", "resourceId": stats_resource, "deletedAt": now(), "status": "STOPPED", "desiredState": "STOPPED"},
                ])
                default_resource = await client.get("/api/v1/resources", headers=headers)
                sorted_resource = await client.get("/api/v1/resources?sortBy=tasks&sortOrder=desc", headers=headers)
                detail_resource = await client.get(f"/api/v1/resources/{stats_resource}", headers=headers)
                for response in (default_resource, sorted_resource, detail_resource):
                    check(response.status_code == 200, "资源统计接口不可用")
                default_item = next(item for item in default_resource.json()["items"] if item["id"] == stats_resource)
                sorted_item = next(item for item in sorted_resource.json()["items"] if item["id"] == stats_resource)
                check(default_item["taskCount"] == sorted_item["taskCount"] == detail_resource.json()["taskCount"] == 1,
                      "资源默认、任务排序和详情统计没有一致排除软删除任务")

                resource_id, task_id = await seed(db, repo, "resource", collecting=True)
                removed = await client.delete(f"/api/v1/resources/{resource_id}?version=1", headers=headers)
                check(removed.status_code == 202, "资源删除未受理")
                linked = await db.tasks.find_one({"id": task_id})
                check(linked.get("deletedAt") and linked.get("resourceDeleted") and linked["desiredState"] == "STOPPED",
                      "资源删除返回时未原子级联任务")
                check(await db.audit.find_one({"action": "delete_resource", "targetId": resource_id}) is not None,
                      "资源删除缺少审计")

                resource_id, task_id = await seed(db, repo, "race")
                removed, started = await asyncio.gather(
                    client.delete(f"/api/v1/tasks/{task_id}?version=1", headers=headers),
                    client.post(f"/api/v1/tasks/{task_id}/start", headers=headers),
                )
                check(removed.status_code == 202 and started.status_code in {202, 409}, "删除与启动竞争返回异常")
                raced = await db.tasks.find_one({"id": task_id})
                check(raced.get("deletedAt") and raced["desiredState"] == "STOPPED", "启动竞争复活已删除任务")
                check(await db.operations.count_documents({"taskId": task_id, "desiredState": "RUNNING",
                                                          "status": "PENDING"}) == 0,
                      "启动竞争留下待执行运行操作")

                resource_id, task_id = await seed(db, repo, "rollback")
                original = repo.audit

                async def fail(*_args, **_kwargs):
                    raise PyMongoError("injected audit failure")

                repo.audit = fail
                try:
                    failed = await client.delete(f"/api/v1/resources/{resource_id}?version=1", headers=headers)
                finally:
                    repo.audit = original
                check(failed.status_code == 503, "审计故障未映射为可重试结果")
                check((await db.resources.find_one({"id": resource_id})).get("deletedAt") is None,
                      "审计回滚后资源仍被删除")
                check((await db.tasks.find_one({"id": task_id})).get("deletedAt") is None,
                      "审计回滚后关联任务仍被删除")
        print(json.dumps({"database": name, "checks": ["explain", "task", "resource-counts", "cascade", "race", "rollback", "artifact"]}, ensure_ascii=False))
    finally:
        await mongo.drop_database(name)
        await mongo.close()
        temporary.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
