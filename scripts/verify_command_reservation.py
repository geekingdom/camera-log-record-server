"""以真实副本集验证定时预算与执行记录的一致性，不建立设备连接。"""

import asyncio
import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from camera_logs.collection.collector import Collector
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.commands import reservation
from camera_logs.common.config import Settings
from pymongo import AsyncMongoClient
from pymongo.errors import ConnectionFailure


class CollectionProxy:
    """在指定写入完成后注入取消，其他操作继续调用真实 MongoDB。"""

    def __init__(self, collection, method):
        self.collection, self.method = collection, method

    def __getattr__(self, name):
        original = getattr(self.collection, name)
        if name != self.method:
            return original

        async def interrupted(*args, **kwargs):
            """故障发生在驱动返回写入结果之后，事务提交之前。"""
            await original(*args, **kwargs)
            raise asyncio.CancelledError()

        return interrupted


class DatabaseProxy:
    """保留真实客户端会话，仅替换单个集合的故障方法。"""

    def __init__(self, db, collection, method):
        self.db, self.client = db, db.client
        self.collection, self.method = collection, method

    def __getattr__(self, name):
        if name == self.collection:
            return CollectionProxy(self.db[name], self.method)
        return getattr(self.db, name)


async def runtime_for(db, identifier):
    """创建无连接、无后台协程的运行实例，调用正式预算入口。"""
    task = {"id": identifier, "runId": identifier, "nodeId": "node", "generation": 1,
            "status": "COLLECTING", "desiredState": "RUNNING", "sessionId": "session",
            "scheduledCommands": [{"id": "periodic", "totalExecutions": 2}]}
    await db.tasks.insert_one(task)
    runtime = object.__new__(SessionRuntime)
    runtime.task, runtime.repo = task, SimpleNamespace(db=db)
    runtime.collector = SimpleNamespace(session_id="session")
    runtime.stopping = runtime.retired = False
    runtime.pending_executions = {}
    return runtime


def details(runtime):
    """按正式采集器回调结构传递不可变身份。"""
    return {"taskId": runtime.task["id"], "runId": runtime.task["runId"],
            "sessionId": runtime.collector.session_id}


async def verify_cancel(db, collection, method):
    """取消后预算和执行记录必须整体回滚，下一次请求仍有完整预算。"""
    runtime = await runtime_for(db, uuid4().hex)
    runtime.repo.db = DatabaseProxy(db, collection, method)
    try:
        await runtime.reserve("periodic", details(runtime))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("故障注入没有取消预算占用")
    budget = await db.budgets.find_one({"_id": runtime.task["runId"] + ":periodic"})
    if budget is not None and budget["attempts"] != 0:
        raise AssertionError("取消后遗留已消耗预算")
    if await db.commands.count_documents({"taskId": runtime.task["id"]}):
        raise AssertionError("取消后遗留发送记录")
    if runtime.pending_executions:
        raise AssertionError("取消后遗留内存执行映射")
    runtime.repo.db = db
    if not await runtime.reserve("periodic", details(runtime)):
        raise AssertionError("取消后不能重新占用预算")
    record = await db.commands.find_one({"taskId": runtime.task["id"]})
    if record["attempt"] != 1:
        raise AssertionError("取消后首次成功占用不是第 1 次")


async def verify_budget(db):
    """不同会话累计、预算耗尽及其他任务预算独立均经过正式入口验证。"""
    runtime = await runtime_for(db, uuid4().hex)
    if not await runtime.reserve("periodic", details(runtime)):
        raise AssertionError("首次占用失败")
    runtime.collector = SimpleNamespace(session_id="reconnected")
    await db.tasks.update_one({"id": runtime.task["id"]}, {"$set": {"sessionId": "reconnected"}})
    if not await runtime.reserve("periodic", details(runtime)):
        raise AssertionError("重连后剩余预算未保留")
    if await runtime.reserve("periodic", details(runtime)):
        raise AssertionError("预算耗尽后仍允许发送")
    records = await db.commands.find({"taskId": runtime.task["id"]}).sort("attempt").to_list()
    if [(r["attempt"], r["sessionId"]) for r in records] != [(1, "session"), (2, "reconnected")]:
        raise AssertionError("执行记录或跨会话次数异常")
    other = await runtime_for(db, uuid4().hex)
    if not await other.reserve("periodic", details(other)):
        raise AssertionError("任务之间共享了执行预算")


async def verify_unknown_commit(db):
    """真实提交后丢失应答，不允许返回发送许可或退款，保留可追踪记录。"""
    runtime = await runtime_for(db, uuid4().hex)
    original = reservation.reservation_transaction

    async def lose_response(repo, callback):
        """写入已提交但调用方无法确认，模拟网络故障的最终驱动结果。"""
        await original(repo, callback)
        raise ConnectionFailure("injected commit response loss")

    writes = []

    async def write(data):
        """仅记录设备写入调用，测试不得建立真实设备连接。"""
        writes.append(data)

    with TemporaryDirectory() as root:
        collector = Collector(runtime.task | {"storageIdentity": "synthetic"}, root,
                              connection_factory=lambda _: None,
                              reserve_execution=runtime.reserve, update_execution=runtime.update_execution)
        collector.session_id = "session"
        collector._connection = SimpleNamespace(write=write)
        collector._accepting_commands = True
        runtime.collector = collector
        sender = asyncio.create_task(collector._sender_loop())
        reservation.reservation_transaction = lose_response
        try:
            await asyncio.wait_for(collector._scheduled_loop(0, {
                "id": "periodic", "command": "probe", "intervalSeconds": .001, "totalExecutions": 1,
            }), 5)
        finally:
            reservation.reservation_transaction = original
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
    if writes:
        raise AssertionError("提交未知时仍写入设备连接")
    budget = await db.budgets.find_one({"_id": runtime.task["runId"] + ":periodic"})
    records = await db.commands.find({"taskId": runtime.task["id"]}).to_list()
    if budget["attempts"] != 1 or len(records) != 1 or records[0]["status"] != "UNKNOWN":
        raise AssertionError("提交未知时预算与执行记录不一致")
    if runtime.pending_executions:
        raise AssertionError("提交未知时不应确认内存发送许可")


async def verify_stop_conflict(db):
    """固定旧快照后提交停止意图，任务条件写必须冲突重试并拒绝旧请求。"""
    runtime = await runtime_for(db, uuid4().hex)

    class StopProxy:
        """仅首次任务写前制造真实事务冲突，重试透传最新状态。"""
        stopped = False

        async def find_one_and_update(self, *args, **kwargs):
            """同一 session 的读取固定快照；事务外停止先提交。"""
            if not self.stopped:
                self.stopped = True
                await db.tasks.find_one({"id": runtime.task["id"]}, session=kwargs["session"])
                await db.tasks.update_one({"id": runtime.task["id"]}, {"$set": {"desiredState": "STOPPED"}})
            return await db.tasks.find_one_and_update(*args, **kwargs)

    runtime.repo.db = SimpleNamespace(client=db.client, tasks=StopProxy(), budgets=db.budgets, commands=db.commands)
    if await runtime.reserve("periodic", details(runtime)):
        raise AssertionError("事务重试覆盖了停止意图")
    if await db.budgets.count_documents({"_id": runtime.task["runId"] + ":periodic"}):
        raise AssertionError("停止冲突后仍扣减预算")
    if await db.commands.count_documents({"taskId": runtime.task["id"]}):
        raise AssertionError("停止冲突后仍创建执行记录")


async def verify_result_retry(db):
    """结果回写瞬态失败时只重试数据库，实际发送一次且保持会话可用。"""
    runtime = await runtime_for(db, uuid4().hex)

    class ResultProxy:
        """前两次结果更新模拟连接失联，其他命令集合操作仍由真实数据库完成。"""
        failures = 0

        async def update_one(self, *args, **kwargs):
            if self.failures < 2:
                self.failures += 1
                raise ConnectionFailure("injected transient result update failure")
            return await db.commands.update_one(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(db.commands, name)

    proxy = ResultProxy()
    runtime.repo.db = SimpleNamespace(client=db.client, tasks=db.tasks, budgets=db.budgets, commands=proxy)
    writes = []

    async def write(data):
        writes.append(data)

    with TemporaryDirectory() as root:
        collector = Collector(runtime.task | {"storageIdentity": "synthetic"}, root,
                              connection_factory=lambda _: None,
                              reserve_execution=runtime.reserve, update_execution=runtime.update_execution)
        collector.session_id = "session"
        collector._connection = SimpleNamespace(write=write)
        collector._accepting_commands = True
        runtime.collector = collector
        sender = asyncio.create_task(collector._sender_loop())
        try:
            await asyncio.wait_for(collector._scheduled_loop(0, {
                "id": "periodic", "command": "probe", "intervalSeconds": .001, "totalExecutions": 1,
            }), 10)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
    budget = await db.budgets.find_one({"_id": runtime.task["runId"] + ":periodic"})
    records = await db.commands.find({"taskId": runtime.task["id"]}).to_list()
    if writes != [b"probe\n"] or budget["attempts"] != 1 or len(records) != 1:
        raise AssertionError("结果回写重试重复发送或重复扣减预算")
    if records[0]["status"] != "SENT" or runtime.pending_executions or proxy.failures != 2:
        raise AssertionError("数据库恢复后未补齐结果")
    if collector._closed.is_set() or not collector._accepting_commands:
        raise AssertionError("短暂回写错误不应关闭采集会话")


async def main():
    """临时库运行完毕总是删除；仅打印验收结果，不输出凭据和设备正文。"""
    settings = Settings()
    client = AsyncMongoClient(settings.mongo_uri, tz_aware=True, w="majority", journal=True)
    name = "command_tx_verify_" + uuid4().hex
    db = client[name]
    try:
        await db.tasks.create_index("id", unique=True)
        await db.commands.create_index("id", unique=True)
        await verify_cancel(db, "budgets", "find_one_and_update")
        await verify_cancel(db, "commands", "insert_one")
        await verify_budget(db)
        await verify_unknown_commit(db)
        await verify_stop_conflict(db)
        await verify_result_retry(db)
        print(json.dumps({"passed": True, "budgetCancellation": True,
                          "executionCancellation": True, "reconnectBudget": True,
                          "unknownCommit": True, "stopConflict": True, "resultRetry": True}))
    finally:
        try:
            await client.drop_database(name)
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())
