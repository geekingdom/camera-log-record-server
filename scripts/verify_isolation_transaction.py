"""在随机临时库中验证节点隔离事务的提交与回滚，绝不访问设备或主库。"""

import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from camera_logs.administration.isolation import confirm_node_isolation
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from fastapi import HTTPException
from pymongo import AsyncMongoClient


def check(condition, message):
    """断言验证条件，失败时保留能定位事务语义的中文说明。"""
    if not condition:
        raise AssertionError(message)


async def documents(collection, query):
    """读取并规范化文档，排除 Mongo 自动生成的内部主键后用于回滚比对。"""
    items = await collection.find(query).to_list(None)
    for item in items:
        item.pop("_id", None)
    return sorted(items, key=lambda item: json.dumps(item, default=str, sort_keys=True))


def case_queries(node_id, task_ids, run_ids, slot_address):
    """返回单个隔离场景涉及的全部集合范围，供事务回滚和冲突后不变量比对。"""
    return {
        "nodes": {"id": node_id},
        "tasks": {"id": {"$in": list(task_ids.values())}},
        "runs": {"id": {"$in": list(run_ids.values())}},
        "endpoint_locks": {"taskId": {"$in": list(task_ids.values())}},
        # 同一设备地址上的名额同时包含待隔离旧运行、后继代次和无关任务；事务必须
        # 只删除明确隔离收据对应的 task/run/generation claim。
        "ssh_connection_slots": {"_id": slot_address},
        "budgets": {"_id": {"$in": [f"{run_id}:scheduled" for run_id in run_ids.values()]}},
        "commands": {"taskId": {"$in": list(task_ids.values())}},
        "operations": {"taskId": {"$in": list(task_ids.values())}},
        "audit": {"targetId": node_id},
        "events": {"nodeId": node_id},
    }


class HeartbeatConflictNodes:
    """首次事务内节点更新前写入新心跳，制造真实副本集写冲突的集合代理。"""
    def __init__(self, collection, node_id):
        self.collection = collection
        self.node_id = node_id
        self.injected = False

    async def update_one(self, *args, **kwargs):
        """只在首个带会话的节点更新前插入外部心跳，后续调用完全透传。"""
        if not self.injected and kwargs.get("session") is not None:
            self.injected = True
            await self.collection.update_one({"id": self.node_id}, {"$set": {"heartbeat": now()}})
        return await self.collection.update_one(*args, **kwargs)

    def __getattr__(self, name):
        """透传节点集合的读取和其他写入 API，保留真实驱动行为。"""
        return getattr(self.collection, name)


class HeartbeatConflictDatabase:
    """仅替换 nodes 属性，其余数据库属性、下标访问和客户端均使用真实对象。"""
    def __init__(self, database, nodes):
        self.database = database
        self.nodes = nodes

    def __getattr__(self, name):
        """将未代理属性交由真实数据库处理。"""
        return getattr(self.database, name)

    def __getitem__(self, name):
        """保持仓储按集合名称下标访问时的真实数据库语义。"""
        return self.nodes if name == "nodes" else self.database[name]


async def seed_case(repo, prefix):
    """建立一个失联节点及三种阻塞任务，覆盖保留和释放运行资源的状态转换。"""
    node_id = f"{prefix}-node"
    task_ids = {
        "running": f"{prefix}-running",
        "paused": f"{prefix}-paused",
        "stopped": f"{prefix}-stopped",
    }
    run_ids = {name: f"{task_id}-run" for name, task_id in task_ids.items()}
    # 三个事务场景在同一临时库依次运行，地址必须隔离，避免前一场景留下的
    # 无关/后继 claim 与下一场景发生主键冲突，从而掩盖回滚语义。
    slot_address = {
        "success": "192.0.2.91",
        "rollback": "192.0.2.92",
        "heartbeat": "192.0.2.93",
    }[prefix]
    await repo.db.nodes.insert_one({
        "id": node_id,
        "heartbeat": now() - timedelta(seconds=31),
        "accepting": True,
        "isolated": False,
    })
    for name, desired_state in (("running", "RUNNING"), ("paused", "PAUSED"), ("stopped", "STOPPED")):
        task_id, run_id = task_ids[name], run_ids[name]
        await repo.db.tasks.insert_one({
            "id": task_id,
            "nodeId": node_id,
            "runId": run_id,
            "generation": 1,
            "status": "BLOCKED",
            "desiredState": desired_state,
            "error": "运行实例已丢失，等待隔离确认",
            "protocol": "SSH",
            "ip": slot_address,
        })
        await repo.db.runs.insert_one({"id": run_id, "taskId": task_id, "startedAt": now()})
        await repo.db.endpoint_locks.insert_one({
            "taskId": task_id,
            "runId": run_id,
            "endpoint": f"127.0.0.1:{2200 + len(name)}",
        })
        await repo.db.budgets.insert_one({"_id": f"{run_id}:scheduled", "attempts": 2})
        await repo.db.commands.insert_many([
            {"id": f"{task_id}-sending", "taskId": task_id, "runId": run_id, "status": "SENDING"},
            {"id": f"{task_id}-queued", "taskId": task_id, "runId": run_id, "status": "QUEUED"},
        ])
        await repo.db.operations.insert_one({
            "id": f"{task_id}-operation",
            "taskId": task_id,
            "desiredState": desired_state,
            "status": "PENDING",
        })
    claims = [
        {"taskId": task_ids[name], "runId": run_ids[name], "generation": 1,
         "nodeId": node_id, "token": f"old-{name}"}
        for name in task_ids
    ]
    claims.extend([
        {"taskId": "unrelated-task", "runId": "unrelated-run", "generation": 1,
         "nodeId": "other-node", "token": "unrelated"},
        {"taskId": task_ids["running"], "runId": "successor-run", "generation": 2,
         "nodeId": "successor-node", "token": "successor"},
    ])
    await repo.db.ssh_connection_slots.insert_one({"_id": slot_address, "claims": claims})
    return node_id, task_ids, run_ids, slot_address


async def verify_success(repo):
    """验证隔离确认提交后任务、命令、锁、运行和审计事件的一致结果。"""
    node_id, task_ids, run_ids, slot_address = await seed_case(repo, "success")
    result = await confirm_node_isolation(repo, node_id, "verification", "temporary transaction check")
    check(result == {"nodeId": node_id, "status": "ISOLATED"}, "隔离确认返回结果不正确")
    node = await repo.db.nodes.find_one({"id": node_id})
    check(node["isolated"] is True and node["accepting"] is False, "节点隔离标记未提交")
    for name in ("running", "paused"):
        task_id, run_id = task_ids[name], run_ids[name]
        task = await repo.db.tasks.find_one({"id": task_id})
        check(task["nodeId"] is None and task["status"] == "PAUSED", f"{name} 任务未释放为暂停")
        check(task["runId"] == run_id, f"{name} 任务的运行标识被改变")
        check(await repo.db.endpoint_locks.find_one({"taskId": task_id, "runId": run_id}) is not None,
              f"{name} 任务的端点锁被错误释放")
        budget = await repo.db.budgets.find_one({"_id": f"{run_id}:scheduled"})
        check(budget == {"_id": f"{run_id}:scheduled", "attempts": 2}, f"{name} 任务的命令预算被改变")
        check((await repo.db.runs.find_one({"id": run_id})).get("endedAt") is None,
              f"{name} 任务的运行被错误结束")
    stopped_id, stopped_run = task_ids["stopped"], run_ids["stopped"]
    stopped_task = await repo.db.tasks.find_one({"id": stopped_id})
    check(stopped_task["nodeId"] is None and stopped_task["status"] == "STOPPED", "停止任务未正确释放")
    check(await repo.db.endpoint_locks.find_one({"taskId": stopped_id, "runId": stopped_run}) is None,
          "停止任务的端点锁未释放")
    check((await repo.db.runs.find_one({"id": stopped_run})).get("endedAt") is not None,
          "停止任务的运行未结束")
    for task_id in task_ids.values():
        sending = await repo.db.commands.find_one({"id": f"{task_id}-sending"})
        queued = await repo.db.commands.find_one({"id": f"{task_id}-queued"})
        check(sending["status"] == "UNKNOWN" and sending.get("completedAt") is not None,
              "发送中的命令未标记为结果未知")
        check(queued["status"] == "CANCELLED" and queued.get("completedAt") is not None,
              "排队命令未取消")
    check(await repo.db.audit.count_documents({"action": "confirm_node_isolation", "targetId": node_id}) == 1,
          "隔离审计未写入")
    check(await repo.db.events.count_documents({"nodeId": node_id, "type": "EXTERNAL_FENCING_CONFIRMED"}) == 1,
          "隔离事件未写入")
    slot = await repo.db.ssh_connection_slots.find_one({"_id": slot_address})
    remaining = {claim["token"] for claim in slot["claims"]}
    check(remaining == {"unrelated", "successor"}, "隔离确认未精确释放目标旧代次 SSH 名额")


async def verify_rollback(repo):
    """通过事务末尾审计异常验证所有此前的隔离写入都会被真实会话回滚。"""
    node_id, task_ids, run_ids, slot_address = await seed_case(repo, "rollback")
    queries = case_queries(node_id, task_ids, run_ids, slot_address)
    before = {name: deepcopy(await documents(repo.db[name], query)) for name, query in queries.items()}

    async def fail_final_audit(*_args, **_kwargs):
        """在事件已经暂存、审计即将写入时注入异常，强制事务中止。"""
        raise RuntimeError("injected final audit failure")

    original_audit = repo.audit
    repo.audit = fail_final_audit
    try:
        try:
            await confirm_node_isolation(repo, node_id, "verification", "rollback transaction check")
        except RuntimeError as error:
            check(str(error) == "injected final audit failure", "回滚场景未到达审计故障点")
        else:
            raise AssertionError("审计故障没有使真实事务失败")
    finally:
        repo.audit = original_audit
    after = {name: await documents(repo.db[name], query) for name, query in queries.items()}
    check(after == before, "审计失败后仍存在未回滚的节点隔离写入")


async def verify_heartbeat_conflict(repo):
    """验证事务重试会重读并拒绝刚恢复心跳的节点，且不会释放任何任务资源。"""
    original_database = repo.db
    node_id, task_ids, run_ids, slot_address = await seed_case(repo, "heartbeat")
    queries = case_queries(node_id, task_ids, run_ids, slot_address)
    before = {name: deepcopy(await documents(original_database[name], query)) for name, query in queries.items()}
    nodes = HeartbeatConflictNodes(original_database.nodes, node_id)
    repo.db = HeartbeatConflictDatabase(original_database, nodes)
    try:
        try:
            await confirm_node_isolation(repo, node_id, "verification", "heartbeat conflict check")
        except HTTPException as error:
            check(error.status_code == 409, "事务重试未拒绝新鲜心跳")
        else:
            raise AssertionError("节点心跳恢复后仍完成了隔离确认")
    finally:
        repo.db = original_database
    check(nodes.injected, "未在首个事务节点写入前注入外部心跳")
    after = {name: await documents(original_database[name], query) for name, query in queries.items()}
    node_before, node_after = before.pop("nodes"), after.pop("nodes")
    check(len(node_before) == len(node_after) == 1, "心跳冲突场景的节点记录数量异常")
    check(node_after[0]["heartbeat"] > node_before[0]["heartbeat"], "外部心跳未成功写入")
    node_after[0].pop("heartbeat")
    node_before[0].pop("heartbeat")
    check(node_after == node_before, "外部心跳以外的节点状态被事务改变")
    check(after == before, "心跳冲突后任务、锁、审计或事件发生了变化")


async def main():
    """连接副本集并运行提交和回滚验证，最终仅删除随机生成的临时数据库。"""
    settings = Settings()
    temporary_database_name = f"camera_logs_isolation_verify_{uuid4().hex}"
    check(temporary_database_name != settings.database_name, "临时数据库名不能是主库名")
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        repo = Repository(client[temporary_database_name], settings)
        await repo.initialize()
        await verify_success(repo)
        await verify_rollback(repo)
        await verify_heartbeat_conflict(repo)
        print(json.dumps({"passed": True, "transactionRollbackVerified": True,
                          "heartbeatConflictVerified": True, "sshSlotsVerified": True}))
    finally:
        await client.drop_database(temporary_database_name)
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
