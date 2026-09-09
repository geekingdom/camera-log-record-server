"""以正式 PATCH 路由验证任务编辑、受控停止操作和审计的副本集事务一致性。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from camera_logs.common import audited_mutations
from camera_logs.common.config import Settings
from camera_logs.common.database import now
from camera_logs.main import create_app
from camera_logs.tasks import editing
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError


def check(condition, message):
    """以中文不变量说明验证失败，不输出临时数据库连接配置。"""
    if not condition:
        raise AssertionError(message)


async def seed(db, repo, name, *, queued=False):
    """写入已认证资源和最小任务；排队场景不创建 Worker 或设备连接。"""
    resource_id, task_id = name + "-resource", name + "-task"
    await db.resources.insert_one({"id": resource_id, "kind": "HIKVISION_NETWORK", "ip": "192.0.2.80",
                                   "model": "verify", "subSerialNumber": name, "authenticatedAt": now(),
                                   "deletedAt": None, "controlClaimVersion": 0})
    await db.tasks.insert_one({"id": task_id, "version": 1, "name": name, "description": "",
                               "protocol": "SSH", "ip": "192.0.2.80", "port": 22, "username": "root",
                               "passwordEncrypted": repo.encrypt("secret"), "hasPassword": True,
                               "resourceId": resource_id, "serialServerResourceId": None,
                               "storageIdentity": name, "initialCommands": [], "scheduledCommands": [],
                               "sourceTemplateId": None, "sourceTemplateVersion": None, "encoding": "utf-8",
                               "loginPrompt": "login:", "passwordPrompt": "Password:", "desiredState": "RUNNING",
                               "status": "STOPPED" if queued else "COLLECTING",
                               "nodeId": None if queued else "verify-node", "generation": 0 if queued else 1,
                               "resourceDeleted": False, "createdAt": now(), "updatedAt": now()})
    if not queued:
        await db.operations.insert_one({"id": name + "-start", "taskId": task_id, "desiredState": "RUNNING",
                                        "status": "PENDING", "action": "start"})
    return task_id, resource_id


async def snapshot(db, task_id, resource_id):
    """读取编辑事务涉及集合，故障时必须没有任何局部提交。"""
    return {"task": await db.tasks.find_one({"id": task_id}), "resource": await db.resources.find_one({"id": resource_id}),
            "operations": await db.operations.find({"taskId": task_id}).to_list(None),
            "audits": await db.audit.find({"targetId": task_id}).to_list(None)}


async def patch(client, task_id, token, port):
    """调用正式编辑路由，不泄露临时鉴权令牌。"""
    return await client.patch(f"/api/v1/tasks/{task_id}", headers={"Authorization": f"Bearer {token}"},
                              json={"version": 1, "port": port})


async def main():
    """在随机库执行回滚和并发验证，最终删除临时库及日志目录。"""
    configured, database_name, token = Settings(), "task_edit_tx_" + uuid4().hex, uuid4().hex
    logs = TemporaryDirectory(prefix="task-edit-transactions-")
    log_path, result = Path(logs.name), None
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    try:
        settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=database_name,
                            encryption_key=Fernet.generate_key().decode(), bootstrap_token=token,
                            admin_password="", start_background=False, log_root=Path(logs.name) / "logs")
        app, db = create_app(settings, mongo[database_name]), mongo[database_name]
        async with app.router.lifespan_context(app):
            repo = app.state.repo
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://verify") as client:
                task_id, resource_id = await seed(db, repo, "rollback")
                before, original = await snapshot(db, task_id, resource_id), repo.audit

                async def fail(*_args, **_kwargs):
                    raise PyMongoError("injected edit audit failure")

                repo.audit = fail
                try:
                    failed = await patch(client, task_id, token, 23)
                finally:
                    repo.audit = original
                check(failed.status_code == 503, "编辑审计失败未返回 503")
                check(before == await snapshot(db, task_id, resource_id), "编辑审计失败留下局部提交")

                task_id, resource_id = await seed(db, repo, "cancel")
                before, original = await snapshot(db, task_id, resource_id), repo.audit

                async def cancel(*_args, **_kwargs):
                    raise asyncio.CancelledError()

                repo.audit = cancel
                try:
                    cancelled = await patch(client, task_id, token, 23)
                finally:
                    repo.audit = original
                check(cancelled.status_code == 500, "编辑取消未返回服务失败")
                check(before == await snapshot(db, task_id, resource_id), "编辑取消留下局部提交")

                task_id, _ = await seed(db, repo, "commit-lost")
                original_transaction = audited_mutations.mutation_transaction

                async def committed_then_lost(transaction_repo, callback):
                    await original_transaction(transaction_repo, callback)
                    raise PyMongoError("injected edit commit acknowledgement loss")

                audited_mutations.mutation_transaction = committed_then_lost
                try:
                    unknown = await patch(client, task_id, token, 23)
                finally:
                    audited_mutations.mutation_transaction = original_transaction
                check(unknown.status_code == 503, "编辑提交确认丢失未返回 503")
                check(await db.operations.count_documents({"taskId": task_id, "action": "edit-stop"}) == 1,
                      "提交确认丢失产生重复编辑停止操作")
                check(await db.audit.count_documents({"targetId": task_id, "action": "edit_task"}) == 1,
                      "提交确认丢失产生重复编辑审计")

                task_id, _ = await seed(db, repo, "concurrent")
                responses = await asyncio.gather(patch(client, task_id, token, 23), patch(client, task_id, token, 24))
                check(sorted(item.status_code for item in responses) == [200, 409], "同版本并发编辑未恰有一次成功")
                operations = await db.operations.find({"taskId": task_id, "action": "edit-stop"}).to_list(None)
                audits = await db.audit.count_documents({"targetId": task_id, "action": "edit_task"})
                check(len(operations) == audits == 1, "并发编辑产生重复停止操作或审计")
                task_id, resource_id = await seed(db, repo, "paused")
                original_prepared = editing._prepared

                async def pause_after_prepare(*args):
                    result = await original_prepared(*args)
                    await db.tasks.update_one({"id": task_id}, {"$set": {"status": "COLLECTING", "desiredState": "PAUSED"}})
                    return result

                editing._prepared = pause_after_prepare
                try:
                    paused = await patch(client, task_id, token, 23)
                finally:
                    editing._prepared = original_prepared
                check(paused.status_code == 409, "预处理后并发暂停没有被事务内重读拒绝")
                check((await db.tasks.find_one({"id": task_id}))["port"] == 22, "暂停竞态覆盖了任务配置")

                task_id, resource_id = await seed(db, repo, "deleted")

                async def delete_after_prepare(*args):
                    result = await original_prepared(*args)
                    await db.resources.update_one({"id": resource_id}, {"$set": {"deletedAt": now()}})
                    return result

                editing._prepared = delete_after_prepare
                try:
                    deleted = await patch(client, task_id, token, 23)
                finally:
                    editing._prepared = original_prepared
                check(deleted.status_code == 409, "预处理后资源软删除没有被声明写 guard 拒绝")
                check((await db.tasks.find_one({"id": task_id}))["port"] == 22, "资源删除竞态覆盖了任务配置")

                task_id, resource_id = await seed(db, repo, "identity")

                async def identity_after_prepare(*args):
                    result = await original_prepared(*args)
                    await db.resources.update_one({"id": resource_id}, {"$set": {"subSerialNumber": "changed"}})
                    return result

                editing._prepared = identity_after_prepare
                try:
                    identity = await patch(client, task_id, token, 23)
                finally:
                    editing._prepared = original_prepared
                check(identity.status_code == 409, "预处理后资源身份变化没有被绑定快照拒绝")
                check((await db.tasks.find_one({"id": task_id}))["port"] == 22, "资源身份变化覆盖了任务配置")
                task_id, _ = await seed(db, repo, "queued", queued=True)
                await db.nodes.insert_one({"id": "queued-node", "heartbeat": now(), "diskPercent": 10,
                                           "accepting": True, "capacity": 100})
                queued = await patch(client, task_id, token, 23)
                queued_task = await db.tasks.find_one({"id": task_id})
                check(queued.status_code == 200, "无归属排队任务编辑未成功")
                check(queued_task["desiredState"] == "RUNNING" and not queued_task.get("restartRequested"),
                      "无归属排队任务编辑错误地停止或请求重启")
                check(await db.operations.count_documents({"taskId": task_id, "action": "edit-stop"}) == 0,
                      "无归属排队任务编辑创建了停止操作")
                await schedule_once(repo)
                queued_task = await db.tasks.find_one({"id": task_id})
                check(queued_task["nodeId"] == "queued-node" and queued_task["status"] == "PENDING",
                      "无归属排队任务编辑后不能被调度领取")
        result = {"passed": True, "realMongoReplicaSet": True, "editRollback": True,
                  "editCancellationRollback": True, "editUnknownCommitVisibleOnce": True,
                  "editConcurrentVersion": True, "editPausedAndDeleteRaces": True,
                  "queuedEditStillSchedulable": True, "noWorkerOrDevice": True}
    finally:
        try:
            await mongo.drop_database(database_name)
            check(database_name not in await mongo.list_database_names(), "临时数据库删除后仍存在")
        finally:
            await mongo.close()
            logs.cleanup()
    check(not log_path.exists(), "临时日志目录删除后仍存在")
    print(json.dumps(result | {"temporaryDatabaseDropped": True, "temporaryLogDirectoryDropped": True}))


if __name__ == "__main__":
    asyncio.run(main())
