"""在随机临时 MongoDB 中验证调度领取事务，不访问设备或主库。"""

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.tasks import claim, scheduler
from pymongo import AsyncMongoClient
from pymongo.errors import ConnectionFailure


def check(condition, message):
    """以中文说明持久化不变量，便于 CI 直接定位失败的事务场景。"""
    if not condition:
        raise AssertionError(message)


class CollectionProxy:
    """仅在指定事务写入前后注入故障，其余 PyMongo 集合 API 原样透传。"""
    def __init__(self, collection, name, hook):
        self.collection = collection
        self.name = name
        self.hook = hook

    async def insert_one(self, *args, **kwargs):
        """锁写入成功但尚未提交时，可模拟调度协程被取消。"""
        result = await self.collection.insert_one(*args, **kwargs)
        await self.hook(self.name, "insert_one", args, kwargs)
        return result

    async def find_one_and_update(self, *args, **kwargs):
        """任务 CAS 成功但尚未提交时，可模拟调度协程被取消。"""
        await self.hook(self.name, "before_find_one_and_update", args, kwargs)
        result = await self.collection.find_one_and_update(*args, **kwargs)
        await self.hook(self.name, "after_find_one_and_update", args, kwargs)
        return result

    async def update_one(self, *args, **kwargs):
        """保留 leader 条件写入和运行记录 upsert 的真实驱动语义。"""
        result = await self.collection.update_one(*args, **kwargs)
        await self.hook(self.name, "update_one", args, kwargs)
        return result

    def __getattr__(self, name):
        """未注入的查询、删除和索引 API 必须继续使用真实集合。"""
        return getattr(self.collection, name)


class DatabaseProxy:
    """按集合名替换代理，同时保留真实 client 以支持 Mongo session。"""
    def __init__(self, database, hooks):
        self.database = database
        self.client = database.client
        self.hooks = hooks

    def __getattr__(self, name):
        """属性形式取得集合时仅对需要注入的集合创建代理。"""
        if name in self.hooks:
            return CollectionProxy(self.database[name], name, self.hooks[name])
        return getattr(self.database, name)

    def __getitem__(self, name):
        """兼容仓储按下标取得集合的访问方式。"""
        if name in self.hooks:
            return CollectionProxy(self.database[name], name, self.hooks[name])
        return self.database[name]


async def seed(repo, prefix, *, paused=False):
    """创建单案例的节点、租约和任务；恢复案例预置旧运行及命令预算。"""
    node_id, task_id = f"{prefix}-node", f"{prefix}-task"
    run_id = f"{prefix}-run" if paused else None
    owner, fence = f"{prefix}-owner", 1
    # 同一临时库串行运行多个案例；leader 固定 _id 必须先重置，不能与前例冲突。
    await repo.db.leaders.delete_many({"_id": "scheduler"})
    await repo.db.nodes.insert_one({
        "id": node_id, "heartbeat": now(), "diskPercent": 5, "accepting": True,
        "capacity": 4, "writeLatencyMs": 0, "inputBytesPerSecond": 0,
    })
    task = {
        "id": task_id, "ip": "127.0.0.1", "port": 2200, "nodeId": None,
        "status": "PAUSED" if paused else "PENDING", "desiredState": "RUNNING",
        "generation": 3 if paused else 0,
    }
    if paused:
        task["runId"] = run_id
        await repo.db.runs.insert_one({"id": run_id, "taskId": task_id, "startedAt": now(), "generation": 3})
        await repo.db.budgets.insert_one({"_id": f"{run_id}:scheduled", "attempts": 2})
        await repo.db.endpoint_locks.insert_one(
            {"endpoint": "127.0.0.1:2200", "taskId": task_id, "runId": run_id}
        )
    await repo.db.tasks.insert_one(task)
    await repo.db.leaders.insert_one({
        "_id": "scheduler", "owner": owner, "fence": fence,
        "expires": now() + timedelta(seconds=30), "claimVersion": 0,
    })
    return task, node_id, {"owner": owner, "fence": fence}


async def state(repo, task_id):
    """读取领取的三项可见结果，取消后可直接断言没有任意局部提交。"""
    task = await repo.db.tasks.find_one({"id": task_id})
    locks = await repo.db.endpoint_locks.find({"taskId": task_id}).to_list(None)
    runs = await repo.db.runs.find({"taskId": task_id}).to_list(None)
    return task, locks, runs


async def assert_consistent(repo, task_id, node_id, expected_run=None):
    """领取成功后 task、lock、run 必须由同一事务共同可见且标识一致。"""
    task, locks, runs = await state(repo, task_id)
    check(
        task["nodeId"] == node_id and task["status"] == "PENDING",
        "任务未处于已领取待启动状态",
    )
    check(len(locks) == len(runs) == 1, "领取后运行锁或运行记录数量不为一")
    check(
        locks[0]["runId"] == task["runId"] == runs[0]["id"],
        "任务、运行锁和运行记录的 runId 不一致",
    )
    check(runs[0]["nodeId"] == node_id, "运行记录没有同步到任务领取节点")
    check(runs[0]["generation"] == task["generation"], "运行记录与任务的领取代次不一致")
    if expected_run:
        check(task["runId"] == expected_run, "暂停恢复错误创建了新的运行")


async def verify_cancelled(repo, point):
    """分别在锁写入和任务 CAS 后取消，真实事务不得留下局部写入。"""
    task, node_id, lease = await seed(repo, f"cancel-{point}")
    raised = False

    async def inject(name, method, _args, kwargs):
        """只拦截带 session 的目标写入，避免种子数据写入触发取消。"""
        nonlocal raised
        matches_lock = point == "lock" and name == "endpoint_locks" and method == "insert_one"
        matches_task = point == "task" and name == "tasks" and method == "after_find_one_and_update"
        if kwargs.get("session") is not None and (matches_lock or matches_task) and not raised:
            raised = True
            raise asyncio.CancelledError()

    original = repo.db
    repo.db = DatabaseProxy(original, {"endpoint_locks": inject, "tasks": inject})
    try:
        try:
            await claim.claim_task(repo, task, node_id, lease=lease, occupied=0)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError(f"{point} 写入后的取消未向上传播")
    finally:
        repo.db = original
    check(raised, f"未到达{point}取消注入点")
    stored, locks, runs = await state(repo, task["id"])
    check(stored["nodeId"] is None and "runId" not in stored, "取消后任务仍保留领取归属")
    check(not locks and not runs, "取消后仍有运行锁或运行记录局部提交")
    leader = await repo.db.leaders.find_one({"_id": "scheduler"})
    check(leader["claimVersion"] == 0, "取消没有回滚本次调度代次写入")
    claimed = await claim.claim_task(repo, stored, node_id, lease=lease, occupied=0)
    check(claimed is not None, "取消回滚后同一任务无法在后续周期领取")
    await assert_consistent(repo, task["id"], node_id)


async def verify_normal_and_resume(repo):
    """验证普通领取可重试，暂停恢复沿用 runId 且命令预算不变。"""
    task, node_id, lease = await seed(repo, "normal")
    claimed = await claim.claim_task(repo, task, node_id, lease=lease, occupied=0)
    check(claimed is not None, "普通任务未领取")
    await assert_consistent(repo, task["id"], node_id)
    duplicate = await claim.claim_task(repo, claimed, node_id, lease=lease, occupied=1)
    check(duplicate is None, "已领取任务被重复领取")

    paused, resume_node, resume_lease = await seed(repo, "resume", paused=True)
    resumed = await claim.claim_task(repo, paused, resume_node, lease=resume_lease, occupied=0)
    check(resumed is not None, "暂停任务未恢复领取")
    await assert_consistent(repo, paused["id"], resume_node, paused["runId"])
    budget = await repo.db.budgets.find_one({"_id": f"{paused['runId']}:scheduled"})
    check(
        budget == {"_id": f"{paused['runId']}:scheduled", "attempts": 2},
        "暂停恢复改变了既有命令预算",
    )


async def verify_unknown_commit(repo):
    """模拟提交已落库但应答断开，后续调度不能重复创建 lock 或 run。"""
    task, node_id, lease = await seed(repo, "unknown")
    original = claim.claim_transaction
    raised = False

    async def committed_then_unknown(*args, **kwargs):
        """先委托真实事务提交，再仅一次伪造网络未知结果。"""
        nonlocal raised
        result = await original(*args, **kwargs)
        if not raised:
            raised = True
            raise ConnectionFailure("injected commit acknowledgement loss")
        return result

    claim.claim_transaction = committed_then_unknown
    try:
        try:
            await claim.claim_task(repo, task, node_id, lease=lease, occupied=0)
        except ConnectionFailure:
            pass
        else:
            raise AssertionError("未知提交模拟未返回连接错误")
    finally:
        claim.claim_transaction = original
    check(raised, "未进入未知提交包装器")
    await assert_consistent(repo, task["id"], node_id)
    await scheduler.schedule_once(repo, lease=lease)
    _task, locks, runs = await state(repo, task["id"])
    check(len(locks) == len(runs) == 1, "提交结果未知后续调度创建了重复运行资源")


async def verify_stale_leases(repo):
    """过期租约和已更换 owner/fence 的旧租约必须在写入前被拒绝。"""
    for mode in ("expired", "replaced"):
        task, node_id, lease = await seed(repo, f"stale-{mode}")
        if mode == "expired":
            await repo.db.leaders.update_one(
                {"_id": "scheduler"}, {"$set": {"expires": now() - timedelta(seconds=1)}}
            )
        else:
            await repo.db.leaders.update_one(
                {"_id": "scheduler"}, {"$set": {"owner": "new-owner", "fence": 2}}
            )
        try:
            await claim.claim_task(repo, task, node_id, lease=lease, occupied=0)
        except claim.SchedulerLeaseLost:
            pass
        else:
            raise AssertionError(f"{mode} 租约仍可领取任务")
        stored, locks, runs = await state(repo, task["id"])
        check(
            stored["nodeId"] is None and not locks and not runs,
            f"{mode} 旧租约留下了部分领取写入",
        )


async def verify_stop_conflict(repo):
    """任务读入事务后收到停止请求，冲突重试不得覆盖用户的最新意图。"""
    task, node_id, lease = await seed(repo, "stop-conflict")
    original, injected = repo.db, False

    async def stop_before_cas(_name, method, _args, kwargs):
        """CAS 前从事务外修改任务，制造真实文档写冲突。"""
        nonlocal injected
        if method == "before_find_one_and_update" and kwargs.get("session") is not None and not injected:
            injected = True
            await original.tasks.update_one({"id": task["id"]}, {"$set": {"desiredState": "STOPPED"}})

    repo.db = DatabaseProxy(original, {"tasks": stop_before_cas})
    try:
        result = await claim.claim_task(repo, task, node_id, lease=lease, occupied=0)
    finally:
        repo.db = original
    check(injected and result is None, "停止竞态没有拒绝领取")
    stored, locks, runs = await state(repo, task["id"])
    check(stored["desiredState"] == "STOPPED" and stored["nodeId"] is None,
          "领取覆盖了用户停止意图")
    check(not locks and not runs, "停止竞态后留下未领取的运行资源")


async def verify_takeover_conflict(repo):
    """在事务首个 leader 条件写前接管，验证重试后旧 fence 不能提交领取。"""
    task, node_id, lease = await seed(repo, "takeover")
    injected = False
    original = repo.db

    async def take_over(name, method, _args, kwargs):
        """在旧快照后接管，再执行条件写以触发真实写冲突与驱动重试。"""
        nonlocal injected
        if (name == "leaders" and method == "before_find_one_and_update"
                and kwargs.get("session") is not None and not injected):
            injected = True
            # session 先固定旧版本；外部更新随后改变同一文档，条件写必须冲突。
            await original.leaders.find_one({"_id": "scheduler"}, session=kwargs["session"])
            await original.leaders.update_one(
                {"_id": "scheduler"},
                {"$set": {"owner": "takeover-owner", "fence": 2, "expires": now() + timedelta(seconds=30)}},
            )

    repo.db = DatabaseProxy(original, {"leaders": take_over})
    try:
        try:
            await claim.claim_task(repo, task, node_id, lease=lease, occupied=0)
        except claim.SchedulerLeaseLost:
            pass
        else:
            raise AssertionError("接管后的旧 leader 条件写仍提交领取")
    finally:
        repo.db = original
    check(injected, "未在事务 leader 条件写前注入接管")
    leader = await original.leaders.find_one({"_id": "scheduler"})
    check(leader["owner"] == "takeover-owner" and leader["fence"] == 2, "接管 leader 被旧事务覆盖")
    stored, locks, runs = await state(repo, task["id"])
    check(
        stored["nodeId"] is None and not locks and not runs,
        "接管冲突后仍有旧 owner 的领取写入",
    )


async def main():
    """为每次运行创建随机库，所有验证完成或失败后均删除并关闭连接。"""
    settings = Settings()
    database_name = f"scheduler_tx_verify_{uuid4().hex}"
    check(database_name != settings.database_name, "调度事务验证不能使用主数据库")
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        repo = Repository(client[database_name], settings)
        await repo.initialize()
        await verify_cancelled(repo, "lock")
        await verify_cancelled(repo, "task")
        await verify_normal_and_resume(repo)
        await verify_unknown_commit(repo)
        await verify_stale_leases(repo)
        await verify_stop_conflict(repo)
        await verify_takeover_conflict(repo)
        print(json.dumps({
            "passed": True,
            "rollbackAfterLockVerified": True,
            "rollbackAfterTaskVerified": True,
            "resumeBudgetVerified": True,
            "unknownCommitVerified": True,
            "leaseFenceVerified": True,
            "takeoverConflictVerified": True,
            "stopConflictVerified": True,
        }))
    finally:
        try:
            await client.drop_database(database_name)
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())
