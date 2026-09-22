"""在真实 Mongo 副本集验证计划重启回收的 token、证据和事务边界。

脚本只创建随机数据库，不启动 Worker 或连接设备。无论结果如何都会删除临时库。
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.node.recovery import _recover_planned_restart_task, recover_planned_restart_tasks
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


def check(condition, message):
    """失败时保留简短、无敏感信息的验证原因。"""
    if not condition:
        raise AssertionError(message)


async def seed_node(repo, token="restart-token"):
    """写入尚未重新准入且已完成关闭的单节点窗口。"""
    await repo.db.nodes.insert_one({
        "id": repo.settings.node_id, "accepting": False, "shutdownToken": token,
        "shuttingDownAt": now(), "shutdownCompletedAt": now(),
    })


async def seed_task(repo, suffix, *, token="restart-token", status="COLLECTING", receipt=True):
    """构造精确归属的旧运行、锁、命令及 SSH 名额。"""
    task_id, run_id, session_id = f"task-{suffix}", f"run-{suffix}", f"session-{suffix}"
    task = {
        "id": task_id, "runId": run_id, "generation": 7, "nodeId": repo.settings.node_id,
        "sessionId": session_id, "protocol": "SSH", "status": status, "desiredState": "RUNNING",
        "plannedRestart": True, "plannedRestartNodeId": repo.settings.node_id,
        "plannedRestartToken": token, "resourceDeleted": False,
    }
    if status == "PENDING":
        task.pop("sessionId")
    if receipt:
        task["closedReceipt"] = {
            "taskId": task_id, "runId": run_id, "generation": 7, "nodeId": repo.settings.node_id,
            "sessionId": session_id, "instanceId": "old-worker", "closedAt": now(),
        }
    await repo.db.tasks.insert_one(task)
    await repo.db.runs.insert_one({"id": run_id, "taskId": task_id, "endedAt": None})
    await repo.db.endpoint_locks.insert_one({"taskId": task_id, "runId": run_id})
    await repo.db.commands.insert_many([
        {"id": f"sending-{suffix}", "taskId": task_id, "runId": run_id, "status": "SENDING"},
        {"id": f"queued-{suffix}", "taskId": task_id, "runId": run_id, "status": "QUEUED"},
    ])
    await repo.db.ssh_connection_slots.insert_one({
        "_id": f"endpoint-{suffix}",
        "claims": [{"taskId": task_id, "runId": run_id, "generation": 7, "token": "slot"}],
    })
    return task


async def verify_success(repo):
    """精确收据应在一次提交中结束旧运行及其全部附属状态。"""
    task = await seed_task(repo, "success")
    check(await recover_planned_restart_tasks(repo, repo.settings.node_id) == 1, "精确收据未被消费")
    current = await repo.db.tasks.find_one({"id": task["id"]})
    commands = {item["status"] async for item in repo.db.commands.find({"taskId": task["id"]})}
    slot = await repo.db.ssh_connection_slots.find_one({"_id": "endpoint-success"})
    run = await repo.db.runs.find_one({"id": task["runId"]})
    check((current["nodeId"], current["status"], current["desiredState"]) == (None, "STOPPED", "RUNNING"),
          "成功回收后的调度状态错误")
    check("plannedRestartToken" not in current and "closedReceipt" not in current, "成功回收残留关闭窗口")
    check(run.get("endedAt") and run.get("endReason") == "PLANNED_NODE_RESTART", "旧 run 未结束")
    check(await repo.db.endpoint_locks.count_documents({"taskId": task["id"]}) == 0, "成功回收遗留端点锁")
    check(commands == {"UNKNOWN", "CANCELLED"}, "命令不确定状态未收尾")
    check(not slot["claims"], "SSH 名额未收回")


async def verify_rejections(repo):
    """旧 token 或没有精确关闭收据时，任何旧运行记录都必须保持不变。"""
    old_token = await seed_task(repo, "old-token", token="previous-token")
    no_receipt = await seed_task(repo, "no-receipt", receipt=False)
    check(await recover_planned_restart_tasks(repo, repo.settings.node_id) == 0, "错误消费旧 token 或无收据连接")
    for task in (old_token, no_receipt):
        run = await repo.db.runs.find_one({"id": task["runId"]})
        lock = await repo.db.endpoint_locks.find_one({"taskId": task["id"], "runId": task["runId"]})
        check(run.get("endedAt") is None and lock is not None, "拒绝场景改变旧运行或锁")


class _TaskCollectionWithFinalCasFailure:
    """仅让最终任务 CAS 返回未命中，验证真实事务回滚此前的收尾写入。"""
    def __init__(self, collection):
        self.collection = collection

    def __getattr__(self, name):
        return getattr(self.collection, name)

    async def update_one(self, query, *args, **kwargs):
        if query.get("plannedRestartToken"):
            return SimpleNamespace(matched_count=0)
        return await self.collection.update_one(query, *args, **kwargs)


class _DatabaseWithFinalCasFailure:
    """代理真实数据库，只替换计划回收的最终任务 CAS。"""
    def __init__(self, database):
        self.database = database
        self.tasks = _TaskCollectionWithFinalCasFailure(database.tasks)

    def __getattr__(self, name):
        return getattr(self.database, name)


async def verify_final_cas_rollback(repo):
    """最终 owner CAS 未命中时，run、lock、命令和 SSH 名额必须一起回滚。"""
    task = await seed_task(repo, "rollback")
    guarded_repo = SimpleNamespace(db=_DatabaseWithFinalCasFailure(repo.db), settings=repo.settings)
    try:
        await _recover_planned_restart_task(guarded_repo, task, repo.settings.node_id, "restart-token")
    except RuntimeError as error:
        check("归属" in str(error), "最终 CAS 未命中没有报告事务中止")
    else:
        raise AssertionError("最终 CAS 未命中意外提交")
    run = await repo.db.runs.find_one({"id": task["runId"]})
    lock = await repo.db.endpoint_locks.find_one({"taskId": task["id"], "runId": task["runId"]})
    commands = {item["status"] async for item in repo.db.commands.find({"taskId": task["id"]})}
    slot = await repo.db.ssh_connection_slots.find_one({"_id": "endpoint-rollback"})
    current = await repo.db.tasks.find_one({"id": task["id"]})
    check(run.get("endedAt") is None and lock is not None, "CAS 失败后 run 或锁未回滚")
    check(commands == {"SENDING", "QUEUED"} and slot["claims"], "CAS 失败后命令或 SSH 名额未回滚")
    check(current.get("plannedRestartToken") == "restart-token", "CAS 失败后任务关闭窗口被清除")


async def main():
    """使用本机真实副本集的随机库执行验证，finally 删除全部自建数据。"""
    configured = Settings()
    database_name = f"planned_restart_verify_{uuid4().hex}"
    check(database_name != configured.database_name, "验证脚本不能使用主数据库")
    client = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                              w="majority", journal=True)
    try:
        hello = await client.admin.command("hello")
        check(bool(hello.get("setName")), "计划重启事务验证需要 MongoDB 副本集")
        settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=database_name,
                            node_id="planned-restart-node", encryption_key=Fernet.generate_key().decode(),
                            start_background=False)
        repo = Repository(client[database_name], settings)
        await repo.initialize()
        await seed_node(repo)
        await verify_success(repo)
        await verify_rejections(repo)
        await verify_final_cas_rollback(repo)
        print(json.dumps({"passed": True, "realMongoReplicaSet": True, "noWorkerOrDevice": True,
                          "tokenAndReceiptRejections": True, "finalCasRollback": True}, ensure_ascii=False))
    finally:
        await client.drop_database(database_name)
        check(database_name not in await client.list_database_names(), "临时验证数据库未删除")
        await client.close()
    print(json.dumps({"temporaryDatabaseDropped": True}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
