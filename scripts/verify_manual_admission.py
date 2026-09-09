"""在临时 MongoDB 副本集库中验证手动命令队列的事务准入。"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from camera_logs.commands import reservation
from camera_logs.commands.manual_admission import admit_manual
from camera_logs.commands.manual_claim import ManualClaimUncertain, claim_manual
from camera_logs.common.config import Settings
from fastapi import HTTPException
from pymongo import AsyncMongoClient
from pymongo.errors import ConnectionFailure

MANUAL_LIMIT = 100
BODY = {"command": "status", "newline": "\n", "delaySeconds": 0, "prompt": None, "timeoutSeconds": 30}


def check(condition, message):
    """以中文断言描述验证失败，方便 CI 直接定位并发不变量。"""
    if not condition:
        raise AssertionError(message)


class CollectionProxy:
    """仅在命令插入完成后取消事务，其他集合行为继续交给真实驱动。"""

    def __init__(self, collection):
        self.collection = collection

    async def insert_one(self, *args, **kwargs):
        """制造提交前取消，确认命令和任务声明版本会一同回滚。"""
        await self.collection.insert_one(*args, **kwargs)
        raise asyncio.CancelledError()

    def __getattr__(self, name):
        """透传 count、find 与其他未注入的 Mongo 集合操作。"""
        return getattr(self.collection, name)


class DatabaseProxy:
    """保留原始 client 的 session 能力，只替换 commands 的插入操作。"""

    def __init__(self, database):
        self.database = database
        self.client = database.client

    def __getattr__(self, name):
        """属性访问 commands 时注入取消，其余集合保持真实副本集行为。"""
        if name == "commands":
            return CollectionProxy(self.database.commands)
        return getattr(self.database, name)

    def __getitem__(self, name):
        """兼容仓储以方括号访问集合的形式。"""
        if name == "commands":
            return CollectionProxy(self.database[name])
        return self.database[name]


def repo(database):
    """构造准入函数所需的最小仓储外形，不初始化服务或任何设备连接。"""
    return SimpleNamespace(db=database)


async def seed_task(database, prefix, *, session_id="session", generation=1):
    """写入一个当前节点拥有、资源已确认可用的采集任务快照。"""
    task = {
        "id": f"{prefix}-task",
        "runId": f"{prefix}-run",
        "nodeId": "manual-verify-node",
        "generation": generation,
        "sessionId": session_id,
        "status": "COLLECTING",
        "desiredState": "RUNNING",
        "resourceDeleted": False,
        "commandClaimVersion": 0,
    }
    await database.tasks.insert_one(task)
    return task


async def admit(database, task, identifier):
    """经正式事务入口提交固定 ID 的模拟手动命令。"""
    return await admit_manual(repo(database), task, identifier, BODY, "manual-verify-actor")


async def verify_concurrent_limit(database):
    """110 个并发提交必须恰好准入 100 条，其余全部明确返回 429。"""
    task = await seed_task(database, "parallel")

    async def one(index):
        try:
            await admit(database, task, f"parallel-command-{index}")
            return "accepted"
        except HTTPException as error:
            if error.status_code == 429:
                return "limited"
            raise AssertionError(f"并发准入返回意外状态 {error.status_code}") from error

    outcomes = await asyncio.gather(*(one(index) for index in range(110)))
    stored = await database.commands.count_documents({
        "taskId": task["id"], "runId": task["runId"], "sessionId": task["sessionId"],
        "kind": "MANUAL", "status": "QUEUED",
    })
    current = await database.tasks.find_one({"id": task["id"]})
    check(outcomes.count("accepted") == MANUAL_LIMIT, "110 个并发请求没有恰好准入 100 条")
    check(outcomes.count("limited") == 10, "超出队列额度的请求没有全部返回 429")
    check(stored == MANUAL_LIMIT, "并发事务后当前会话队列数量不等于 100")
    check(current["commandClaimVersion"] == MANUAL_LIMIT, "拒绝事务没有回滚任务声明版本")


async def verify_session_and_task_isolation(database):
    """同任务旧会话和另一任务的积压都不能占用当前会话的额度。"""
    first = await seed_task(database, "isolation", session_id="current")
    second = await seed_task(database, "other")
    await database.commands.insert_many([{
        "id": f"old-session-{index}", "taskId": first["id"], "runId": first["runId"],
        "sessionId": "previous", "kind": "MANUAL", "status": "QUEUED",
    } for index in range(MANUAL_LIMIT)])
    first_record = await admit(database, first, "current-command")
    second_record = await admit(database, second, "other-command")
    first_current = await database.commands.count_documents({
        "taskId": first["id"], "runId": first["runId"], "sessionId": "current",
        "kind": "MANUAL", "status": "QUEUED",
    })
    second_current = await database.commands.count_documents({
        "taskId": second["id"], "runId": second["runId"], "sessionId": second["sessionId"],
        "kind": "MANUAL", "status": "QUEUED",
    })
    check(first_record["id"] == "current-command" and second_record["id"] == "other-command",
          "独立会话或任务的命令没有按固定 ID 写入")
    check(first_current == second_current == 1, "旧会话或另一任务错误占用了当前会话额度")


async def verify_stale_snapshots_rejected(database):
    """会话或 owner/generation 已变化的旧 API 快照必须返回 409 且不得插入。"""
    for field, value in (("sessionId", "successor"), ("generation", 2)):
        task = await seed_task(database, f"stale-{field}")
        await database.tasks.update_one({"id": task["id"]}, {"$set": {field: value}})
        try:
            await admit(database, task, f"stale-command-{field}")
        except HTTPException as error:
            check(error.status_code == 409, "过期会话或 owner 没有返回 409")
        else:
            raise AssertionError("过期会话或 owner 快照仍可插入手动命令")
        check(await database.commands.count_documents({"taskId": task["id"]}) == 0,
              "过期快照冲突后仍插入了手动命令")


async def verify_cancel_rollback(database):
    """commands 插入后取消事务时，命令和 commandClaimVersion 必须同时回滚。"""
    task = await seed_task(database, "cancel")
    original_repo = repo(database)
    interrupted_repo = repo(DatabaseProxy(database))
    try:
        await admit_manual(interrupted_repo, task, "cancelled-command", BODY, "manual-verify-actor")
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("插入后的取消没有从事务向上传播")
    check(await database.commands.count_documents({"taskId": task["id"]}) == 0,
          "事务取消后仍留下手动命令")
    current = await database.tasks.find_one({"id": task["id"]})
    check(current["commandClaimVersion"] == 0, "事务取消后 commandClaimVersion 没有回滚")
    record = await admit_manual(original_repo, task, "retry-command", BODY, "manual-verify-actor")
    current = await database.tasks.find_one({"id": task["id"]})
    check(record["id"] == "retry-command", "取消回滚后固定 ID 重试未成功")
    check(current["commandClaimVersion"] == 1, "成功重试后的 commandClaimVersion 不正确")


async def verify_claim_stop_conflict(database):
    """事务固定旧快照后提交停止意图，重试不得把命令领取为 SENDING。"""
    task = await seed_task(database, "claim-stop")
    command = await admit(database, task, "claim-stop-command")
    injected = False

    class StopTaskProxy:
        """仅在第一次任务条件写前从事务外提交停止，制造真实写冲突。"""

        async def find_one_and_update(self, *args, **kwargs):
            nonlocal injected
            if not injected and kwargs.get("session") is not None:
                injected = True
                await database.tasks.find_one({"id": task["id"]}, session=kwargs["session"])
                await database.tasks.update_one({"id": task["id"]}, {"$set": {"desiredState": "STOPPED"}})
            return await database.tasks.find_one_and_update(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(database.tasks, name)

    claim_repo = repo(SimpleNamespace(client=database.client, tasks=StopTaskProxy(), commands=database.commands))
    claimed = await claim_manual(claim_repo, task, task["sessionId"], command)
    stored_task = await database.tasks.find_one({"id": task["id"]})
    stored_command = await database.commands.find_one({"id": command["id"]})
    check(injected, "没有在手动领取任务 CAS 前注入停止竞争")
    check(claimed is None, "停止竞争后旧实例仍领取了手动命令")
    check(stored_task["desiredState"] == "STOPPED" and stored_task["commandClaimVersion"] == 1,
          "停止竞争后任务状态或已提交的准入版本不正确")
    check(stored_command["status"] == "QUEUED", "停止竞争后命令被错误改为 SENDING")


async def verify_claim_cancel_rollback(database):
    """命令 CAS 后取消时，任务声明版本和命令状态必须整体回滚。"""
    task = await seed_task(database, "claim-cancel")
    command = await admit(database, task, "claim-cancel-command")

    class ClaimCommandProxy:
        """命令领取写入后取消，验证真实事务不会留下部分状态转换。"""

        async def find_one_and_update(self, *args, **kwargs):
            await database.commands.find_one_and_update(*args, **kwargs)
            raise asyncio.CancelledError()

        def __getattr__(self, name):
            return getattr(database.commands, name)

    claim_repo = repo(SimpleNamespace(client=database.client, tasks=database.tasks, commands=ClaimCommandProxy()))
    try:
        await claim_manual(claim_repo, task, task["sessionId"], command)
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("命令领取写入后的取消没有向上传播")
    stored_task = await database.tasks.find_one({"id": task["id"]})
    stored_command = await database.commands.find_one({"id": command["id"]})
    check(stored_task["commandClaimVersion"] == 1, "领取取消后额外的任务声明版本没有回滚")
    check(stored_command["status"] == "QUEUED", "领取取消后命令状态没有回滚为 QUEUED")


async def verify_claim_unknown_commit(database):
    """提交确认丢失时只能报告未知，函数内部不得再次领取同一命令。"""
    task = await seed_task(database, "claim-unknown")
    command = await admit(database, task, "claim-unknown-command")
    original = reservation.reservation_transaction
    calls = 0

    async def committed_then_unknown(*args, **kwargs):
        """先让真实事务提交，再模拟驱动丢失提交确认。"""
        nonlocal calls
        calls += 1
        await original(*args, **kwargs)
        raise ConnectionFailure("injected commit acknowledgement loss")

    reservation.reservation_transaction = committed_then_unknown
    try:
        try:
            await claim_manual(repo(database), task, task["sessionId"], command)
        except ManualClaimUncertain:
            pass
        else:
            raise AssertionError("提交确认丢失没有转换为 ManualClaimUncertain")
    finally:
        reservation.reservation_transaction = original
    stored_task = await database.tasks.find_one({"id": task["id"]})
    stored_command = await database.commands.find_one({"id": command["id"]})
    check(calls == 1, "提交结果未知时 claim_manual 在函数内部重复领取")
    check(stored_task["commandClaimVersion"] == 2, "未知提交没有留下单次领取版本")
    check(stored_command["status"] == "SENDING", "未知提交没有保留已提交的 SENDING 状态")


async def main():
    """使用随机临时库执行全部验证，finally 中必定 drop，不访问真实设备。"""
    settings = Settings()
    database_name = f"manual_admission_verify_{uuid4().hex}"
    check(database_name != settings.database_name, "验证脚本不能使用配置的主数据库")
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                              w="majority", journal=True)
    try:
        database = client[database_name]
        await database.commands.create_index("id", unique=True)
        await verify_concurrent_limit(database)
        await verify_session_and_task_isolation(database)
        await verify_stale_snapshots_rejected(database)
        await verify_cancel_rollback(database)
        await verify_claim_stop_conflict(database)
        await verify_claim_cancel_rollback(database)
        await verify_claim_unknown_commit(database)
        print(json.dumps({
            "passed": True,
            "concurrentAdmission": True,
            "sessionAndTaskIsolation": True,
            "staleSnapshotRejected": True,
            "insertCancellationRollback": True,
            "claimStopConflict": True,
            "claimCancellationRollback": True,
            "claimUnknownCommit": True,
        }))
    finally:
        try:
            await client.drop_database(database_name)
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())
