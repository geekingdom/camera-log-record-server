"""在随机真实 Mongo 副本集库验证 BLOCKED 关闭收据的事务收尾，不访问设备。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.node.recovery import finalize_closed_task
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


def check(condition, message):
    """失败时给出可定位的中文断言，避免把临时库验证误报为设备问题。"""
    if not condition:
        raise AssertionError(message)


async def seed(repo, suffix, *, restart=False, generation=1):
    """写入精确关闭收据、旧运行、锁、命令和对应控制操作。"""
    task_id, run_id, node_id = f"task-{suffix}", f"run-{suffix}", f"node-{suffix}"
    operation_id = f"operation-{suffix}"
    task = {
        "id": task_id, "runId": run_id, "nodeId": node_id, "generation": generation,
        "sessionId": f"session-{suffix}", "status": "BLOCKED", "desiredState": "STOPPED",
        "restartRequested": restart, "controlOperationId": operation_id,
    }
    if restart:
        task["resourceId"] = f"resource-{suffix}"
        await repo.db.resources.insert_one({"id": task["resourceId"], "deletedAt": None, "healthStatus": "ONLINE"})
    receipt = {"taskId": task_id, "runId": run_id, "nodeId": node_id, "generation": generation,
               "sessionId": task["sessionId"], "instanceId": "verify-worker", "closedAt": now()}
    await repo.db.tasks.insert_one(task | {"closedReceipt": receipt})
    await repo.db.runs.insert_one({"id": run_id, "taskId": task_id})
    await repo.db.endpoint_locks.insert_one({"taskId": task_id, "runId": run_id})
    await repo.db.commands.insert_many([
        {"id": f"sending-{suffix}", "taskId": task_id, "runId": run_id, "status": "SENDING"},
        {"id": f"queued-{suffix}", "taskId": task_id, "runId": run_id, "status": "QUEUED"},
    ])
    await repo.db.operations.insert_one({"id": operation_id, "taskId": task_id,
                                         "action": "restart-blocked" if restart else "stop",
                                         "desiredState": "RUNNING" if restart else "STOPPED", "status": "PENDING"})
    return task


async def verify_finalization(repo):
    """验证停止完成、恢复待新会话和旧代次 CAS 三个事务边界。"""
    stopped = await seed(repo, "stop")
    check(await finalize_closed_task(repo, stopped), "停止收据未被事务消费")
    current = await repo.db.tasks.find_one({"id": stopped["id"]})
    operation = await repo.db.operations.find_one({"id": stopped["controlOperationId"]})
    statuses = {item["status"] async for item in repo.db.commands.find({"taskId": stopped["id"]})}
    check((current["status"], current["desiredState"], current["nodeId"]) == ("STOPPED", "STOPPED", None), "停止任务状态错误")
    check(operation["status"] == "SUCCEEDED" and statuses == {"UNKNOWN", "CANCELLED"}, "停止命令或操作收尾错误")
    check(await repo.db.endpoint_locks.count_documents({"taskId": stopped["id"]}) == 0, "停止遗留端点锁")

    restart = await seed(repo, "restart", restart=True)
    check(await finalize_closed_task(repo, restart), "恢复收据未被事务消费")
    current = await repo.db.tasks.find_one({"id": restart["id"]})
    operation = await repo.db.operations.find_one({"id": restart["controlOperationId"]})
    check((current["status"], current["desiredState"], current["nodeId"]) == ("STOPPED", "RUNNING", None), "恢复任务未重新排队")
    check(operation["status"] == "PENDING", "旧运行释放时过早完成恢复操作")

    stale = await seed(repo, "stale")
    await repo.db.tasks.update_one({"id": stale["id"]}, {"$set": {"generation": 2}})
    check(not await finalize_closed_task(repo, stale), "旧代次错误消费后继任务收据")
    check(await repo.db.endpoint_locks.count_documents({"taskId": stale["id"]}) == 1, "旧代次删除后继锁")


async def main():
    """只使用配置 Mongo 的新随机库；finally 无条件清理库和临时日志目录。"""
    configured = Settings()
    database_name = f"blocked_recovery_verify_{uuid4().hex}"
    check(database_name != configured.database_name, "验证脚本不能使用主数据库")
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    try:
        with TemporaryDirectory(prefix="camera-blocked-recovery-") as temporary:
            settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=database_name,
                                log_root=Path(temporary) / "logs", node_id="verify-node",
                                encryption_key=Fernet.generate_key().decode(), start_background=False)
            repo = Repository(mongo[database_name], settings)
            await repo.initialize()
            await verify_finalization(repo)
            print(json.dumps({"passed": True, "realMongoReplicaSet": True, "noWorkerOrDevice": True}))
    finally:
        await mongo.drop_database(database_name)
        check(database_name not in await mongo.list_database_names(), "临时验证数据库未删除")
        await mongo.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryDirectoryRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
