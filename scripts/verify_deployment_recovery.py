"""真实副本集验证受控部署证明、文件级旧会话恢复与事务回滚。"""

import asyncio
import json
import os
from types import SimpleNamespace
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.node.deployment_recovery import complete_deployment_recovery, prepare_deployment_recovery
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


def check(value, message):
    """以无敏感信息的固定说明报告验证失败。"""
    if not value:
        raise AssertionError(message)


async def seed(repo, suffix, *, paused=False, legacy=False):
    """构造一个含当前与旧会话 OPEN 文件的精确旧 owner。"""
    task, run = f"task-{suffix}", f"run-{suffix}"
    await repo.db.tasks.insert_one({"id": task, "runId": run, "nodeId": repo.settings.node_id,
        "generation": 1, "sessionId": "current", "desiredState": "PAUSED" if paused else "RUNNING",
        "status": "PAUSED" if paused else "COLLECTING", "controlClaimVersion": 1,
        "controlOperationId": "control", "protocol": "SSH", "ip": "192.0.2.1", "port": 22})
    document = {"id": run, "taskId": task, "nodeId": repo.settings.node_id, "generation": 1}
    if not legacy:
        document["sessionId"] = "current"
    await repo.db.runs.insert_one(document)
    await repo.db.endpoint_locks.insert_one({"taskId": task, "runId": run})
    await repo.db.commands.insert_many([{"id": f"sending-{suffix}", "taskId": task, "runId": run, "status": "SENDING"},
                                        {"id": f"queued-{suffix}", "taskId": task, "runId": run, "status": "QUEUED"}])
    await repo.db.ssh_connection_slots.insert_one({"_id": f"slot-{suffix}", "claims": [{"taskId": task, "runId": run, "generation": 1}]})
    for session in ("old", "current"):
        await repo.db.files.insert_one({"id": f"file-{suffix}-{session}", "taskId": task, "runId": run,
            "nodeId": repo.settings.node_id, "sessionId": session, "status": "OPEN",
            "path": f"/tmp/{suffix}-{session}.log", "bytes": 10, "createdAt": now()})
    return task, run


class _FinalTaskCasFailure:
    """只让 release 的最终任务 CAS 未命中，前序写入必须由真实事务回滚。"""
    def __init__(self, collection): self.collection = collection
    def __getattr__(self, name): return getattr(self.collection, name)
    async def update_one(self, query, *args, **kwargs):
        if query.get("desiredState") == "RUNNING" and query.get("deploymentRecovery.token"):
            return SimpleNamespace(matched_count=0)
        return await self.collection.update_one(query, *args, **kwargs)


class _DatabaseProxy:
    """真实数据库代理，仅注入最终 CAS 未命中。"""
    def __init__(self, database): self.database, self.tasks = database, _FinalTaskCasFailure(database.tasks)
    def __getattr__(self, name): return getattr(self.database, name)


async def main():
    """执行真实事务和 Explain，结束删除随机库。"""
    uri, name = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27129/?replicaSet=rs0"), f"deployment_recovery_verify_{uuid4().hex}"
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    try:
        check(bool((await client.admin.command("hello")).get("setName")), "需要 MongoDB 副本集")
        repo = Repository(client[name], Settings(_env_file=None, mongo_uri=uri, database_name=name, node_id="verify-node",
                          encryption_key=Fernet.generate_key().decode(), start_background=False))
        await repo.initialize()
        _, run = await seed(repo, "success", legacy=True)
        prepared = await prepare_deployment_recovery(repo, repo.settings.node_id, token="a" * 32)
        check(prepared["prepared"] == 1, "prepare 未冻结旧 owner")
        await complete_deployment_recovery(repo, repo.settings.node_id, "a" * 32)
        stored = await repo.db.runs.find_one({"id": run})
        files = [row async for row in repo.db.files.find({"runId": run})]
        check(stored["sessionId"] == "current" and {row["deploymentClosedProof"]["sessionId"] for row in files} == {"old", "current"}, "旧会话文件证明错误")
        paused_task, paused_run = await seed(repo, "paused", paused=True)
        await prepare_deployment_recovery(repo, repo.settings.node_id, token="b" * 32)
        await complete_deployment_recovery(repo, repo.settings.node_id, "b" * 32)
        check((await repo.db.tasks.find_one({"id": paused_task}))["nodeId"] is None and not (await repo.db.runs.find_one({"id": paused_run})).get("endedAt"), "暂停破坏运行预算")
        rollback_task, rollback_run = await seed(repo, "rollback")
        await prepare_deployment_recovery(repo, repo.settings.node_id, token="c" * 32)
        guarded = SimpleNamespace(db=_DatabaseProxy(repo.db), settings=repo.settings)
        try:
            await complete_deployment_recovery(guarded, repo.settings.node_id, "c" * 32)
        except RuntimeError:
            pass
        else:
            raise AssertionError("最终 CAS 故障未触发")
        check((await repo.db.runs.find_one({"id": rollback_run})).get("deploymentRecovery"), "最终 CAS 后 run 未回滚")
        check(await repo.db.endpoint_locks.find_one({"taskId": rollback_task}), "最终 CAS 后锁未回滚")
        explain = await repo.db.command("explain", {"find": "files", "filter": {"deploymentClosedProof.nodeId": repo.settings.node_id,
            "deploymentClosedProof.token": {"$exists": True}, "status": "OPEN"}}, verbosity="executionStats")
        check("IXSCAN" in json.dumps(explain["queryPlanner"]["winningPlan"]), "文件 proof 查询未命中索引")
        print(json.dumps({"passed": True, "realMongoReplicaSet": True, "fileSessionProof": True, "finalCasRollback": True, "pausedBudget": True}, ensure_ascii=False))
    finally:
        await client.drop_database(name)
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
