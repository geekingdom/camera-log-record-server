"""以正式任务控制 API 验证操作、审计与运行状态的 MongoDB 事务一致性。"""

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
from camera_logs.tasks import control
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.errors import ConnectionFailure, PyMongoError


def check(condition, message):
    """用中文报告事务不变量失败，避免在日志中输出临时认证材料。"""
    if not condition:
        raise AssertionError(message)


def headers(token):
    """临时 bootstrap token 只在内存中用于正式路由认证。"""
    return {"Authorization": f"Bearer {token}"}


async def seed(db, name, *, desired="STOPPED", status="STOPPED", paused=False):
    """写入最小已认证资源和任务夹具，不触发 Worker 或任何设备连接。"""
    resource_id, task_id = f"{name}-resource", f"{name}-task"
    await db.resources.insert_one({"id": resource_id, "version": 1, "deletedAt": None,
                                   "kind": "HIKVISION_NETWORK", "ip": "192.0.2.20"})
    task = {"id": task_id, "resourceId": resource_id, "serialServerResourceId": None,
            "protocol": "SSH", "ip": "192.0.2.20", "port": 22, "desiredState": desired,
            "status": status, "nodeId": None, "generation": 1, "resourceDeleted": False,
            "createdAt": now(), "updatedAt": now()}
    if paused:
        task["runId"] = f"{name}-run"
        await db.runs.insert_one({"id": task["runId"], "taskId": task_id, "startedAt": now()})
        await db.endpoint_locks.insert_one({"taskId": task_id, "runId": task["runId"], "endpoint": "192.0.2.20:22"})
        await db.budgets.insert_one({"_id": f"{task['runId']}:scheduled", "attempts": 2})
    await db.tasks.insert_one(task)
    return task


async def post(client, task_id, intent, token):
    """请求正式控制路由，返回 API 响应而不泄露其认证头。"""
    return await client.post(f"/api/v1/tasks/{task_id}/{intent}", headers=headers(token))


async def snapshot(db, task_id, resource_id):
    """读取控制事务涉及的全部集合，用于故障后逐项比较。"""
    return {
        "task": await db.tasks.find_one({"id": task_id}),
        "resource": await db.resources.find_one({"id": resource_id}),
        "operations": await db.operations.find({"taskId": task_id}).to_list(None),
        "locks": await db.endpoint_locks.find({"taskId": task_id}).to_list(None),
        "runs": await db.runs.find({"taskId": task_id}).to_list(None),
        "budgets": await db.budgets.find({}).to_list(None),
        "audits": await db.audit.find({"targetId": task_id}).to_list(None),
    }


async def assert_equal(before, after, message):
    """忽略 Mongo `_id` 的稳定性以外字段，事务失败不得留下任意业务差异。"""
    for key in before:
        check(before[key] == after[key], f"{message}：{key} 发生局部提交")


async def inject_failure(repo, operation, *, cancelled=False):
    """在审计点注入驱动错误或取消，业务写仍先经真实 Mongo 事务执行。"""
    original = repo.audit

    async def fail(*_args, **_kwargs):
        if cancelled:
            raise asyncio.CancelledError()
        raise PyMongoError("injected task-control audit failure")

    repo.audit = fail
    try:
        return await operation()
    finally:
        repo.audit = original


async def verify_rollback(client, repo, db, token):
    """审计错误和取消均回滚 task、operation、资源 claim 与运行关联集合。"""
    for suffix, cancelled in (("audit", False), ("cancel", True)):
        task = await seed(db, f"rollback-{suffix}", desired="PAUSED", status="PAUSED", paused=True)
        before = await snapshot(db, task["id"], task["resourceId"])
        response = None
        try:
            response = await inject_failure(
                repo, lambda task_id=task["id"]: post(client, task_id, "stop", token), cancelled=cancelled
            )
        except asyncio.CancelledError:
            check(cancelled, "非取消场景错误传播为取消")
        else:
            expected = 500 if cancelled else 503
            check(response.status_code == expected, "审计失败或路由取消没有返回预期失败状态")
        after = await snapshot(db, task["id"], task["resourceId"])
        await assert_equal(before, after, f"{suffix} 后")

    task = await seed(db, "rollback-opposite")
    accepted = await post(client, task["id"], "start", token)
    check(accepted.status_code == 202, "相反意图回滚夹具未建立启动操作")
    before = await snapshot(db, task["id"], task["resourceId"])
    failed = await inject_failure(repo, lambda: post(client, task["id"], "stop", token))
    check(failed.status_code == 503, "相反意图审计失败没有返回 503")
    await assert_equal(before, await snapshot(db, task["id"], task["resourceId"]), "相反 PENDING 取消失败")


async def verify_direct_cancellation(repo, db):
    """直接事务调用必须传播取消，且不得留下控制操作或资源 claim。"""
    task = await seed(db, "direct-cancel")
    before = await snapshot(db, task["id"], task["resourceId"])
    original = repo.audit

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    repo.audit = cancelled
    try:
        try:
            await control.request_control(repo, task["id"], "RUNNING", {"id": "bootstrap", "scopes": ["*"]})
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("直接控制事务没有传播取消")
    finally:
        repo.audit = original
    await assert_equal(before, await snapshot(db, task["id"], task["resourceId"]), "直接取消")


async def verify_same_intent(client, db, token):
    """八个并发相同启动请求必须复用同一待完成 operation 和唯一审计。"""
    task = await seed(db, "same-start")
    responses = await asyncio.gather(*[post(client, task["id"], "start", token) for _ in range(8)])
    check(all(item.status_code == 202 for item in responses), "并发相同启动请求出现非 202")
    identifiers = {item.json()["id"] for item in responses}
    check(len(identifiers) == 1, "并发相同启动没有复用同一 operation")
    identifier = identifiers.pop()
    check(await db.operations.count_documents({"taskId": task["id"]}) == 1, "并发相同启动创建多个 operation")
    check(await db.audit.count_documents({"targetId": task["id"], "action": "control:RUNNING"}) == 1,
          "并发相同启动创建重复审计")
    stored = await db.tasks.find_one({"id": task["id"]})
    check(stored["desiredState"] == "RUNNING" and stored["controlOperationId"] == identifier,
          "并发相同启动没有留下唯一任务操作指针")


async def verify_opposites(client, db, token):
    """并发相反意图最终只能留一个与任务 desiredState 一致的待完成操作。"""
    task = await seed(db, "opposites")
    responses = await asyncio.gather(*[
        *(post(client, task["id"], "start", token) for _ in range(4)),
        *(post(client, task["id"], "stop", token) for _ in range(4)),
    ])
    check(all(item.status_code == 202 for item in responses), "并发相反控制请求出现非 202")
    stored = await db.tasks.find_one({"id": task["id"]})
    pending = await db.operations.find({"taskId": task["id"], "status": "PENDING"}).to_list(None)
    check(len(pending) <= 1, "并发相反意图留下多个 PENDING operation")
    if pending:
        check(pending[0]["desiredState"] == stored["desiredState"] == "RUNNING",
              "最终 PENDING operation 与任务 desiredState 不一致")
        check(stored["controlOperationId"] == pending[0]["id"], "最终 operation 未被任务指针引用")
    cancelled = await db.operations.count_documents({"taskId": task["id"], "status": "CANCELLED"})
    check(cancelled >= 1 or len({item.json()["id"] for item in responses}) == 1,
          "相反意图既未取消旧 operation 也未复用单一 operation")


async def verify_paused_stop(client, repo, db, token):
    """无节点暂停停止同步结束运行和释放匹配锁，预算保持且失败完整回滚。"""
    task = await seed(db, "paused-stop", desired="PAUSED", status="PAUSED", paused=True)
    before = await snapshot(db, task["id"], task["resourceId"])
    failed = await inject_failure(repo, lambda: post(client, task["id"], "stop", token))
    check(failed.status_code == 503, "暂停停止审计失败没有返回 503")
    await assert_equal(before, await snapshot(db, task["id"], task["resourceId"]), "暂停停止审计失败")
    response = await post(client, task["id"], "stop", token)
    check(response.status_code == 202 and response.json()["status"] == "SUCCEEDED", "暂停停止没有同步成功")
    stored = await db.tasks.find_one({"id": task["id"]})
    run = await db.runs.find_one({"id": task["runId"]})
    check(stored["desiredState"] == stored["status"] == "STOPPED" and run.get("endedAt"), "暂停停止未结束运行")
    check(await db.endpoint_locks.count_documents({"taskId": task["id"]}) == 0, "暂停停止未释放匹配锁")
    check(await db.budgets.find_one({"_id": f"{task['runId']}:scheduled"}) == {"_id": f"{task['runId']}:scheduled", "attempts": 2},
          "暂停停止改变了既有预算")


async def verify_resource_delete_race(app, client, db, token):
    """真实删除 API 与迟到启动交错后，不得在已删除资源上留下 RUNNING 意图。"""
    task = await seed(db, "delete-race")
    transport = httpx.ASGITransport(app=app, client=("198.51.100.20", 45001), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://verify") as deleter:
        start, deleted = await asyncio.gather(
            post(client, task["id"], "start", token),
            deleter.delete(f"/api/v1/resources/{task['resourceId']}?version=1", headers=headers(token)),
        )
    check(start.status_code in {202, 409} and deleted.status_code == 202, "资源删除与启动竞态返回意外状态")
    resource = await db.resources.find_one({"id": task["resourceId"]})
    stored = await db.tasks.find_one({"id": task["id"]})
    check(not (resource.get("deletedAt") and stored["desiredState"] == "RUNNING"),
          "资源删除后仍留下新的 RUNNING 控制意图")

    control_first = await seed(db, "delete-after-control")
    started = await post(client, control_first["id"], "start", token)
    check(started.status_code == 202, "删除后控制顺序夹具未接受启动")
    deleted = await client.delete(f"/api/v1/resources/{control_first['resourceId']}?version=1", headers=headers(token))
    check(deleted.status_code == 202, "控制先提交后的资源删除失败")
    stored = await db.tasks.find_one({"id": control_first["id"]})
    check(stored["desiredState"] == "STOPPED" and stored["resourceDeleted"], "删除扫尾没有覆盖先前控制意图")

    delete_first = await seed(db, "delete-before-control")
    deleted = await client.delete(f"/api/v1/resources/{delete_first['resourceId']}?version=1", headers=headers(token))
    check(deleted.status_code == 202, "控制前资源删除失败")
    rejected = await post(client, delete_first["id"], "start", token)
    check(rejected.status_code == 409, "资源先删除后控制没有拒绝迟到启动")


async def verify_unknown_ack(client, db, token):
    """提交 ACK 丢失只确认固定 operation 与任务指针，不能重做控制写入。"""
    task = await seed(db, "unknown-ack")
    original, calls = audited_mutations.mutation_transaction, 0

    async def committed_then_lost(repo, callback):
        nonlocal calls
        calls += 1
        await original(repo, callback)
        raise ConnectionFailure("injected commit acknowledgement loss")

    audited_mutations.mutation_transaction = committed_then_lost
    try:
        response = await post(client, task["id"], "start", token)
    finally:
        audited_mutations.mutation_transaction = original
    check(response.status_code == 202 and calls == 1, "ACK 丢失后控制请求没有按固定 operation 恢复")
    operation = response.json()
    stored = await db.tasks.find_one({"id": task["id"]})
    check(stored["controlOperationId"] == operation["id"], "ACK 恢复返回的 operation 未由任务指针确认")
    check(await db.operations.count_documents({"taskId": task["id"]}) == 1, "ACK 丢失后重复执行控制写入")


async def verify_auto_start_creation(client, repo, db, token):
    """自动启动创建须原子包含任务、成功映射、启动操作和两条审计。"""
    resource_id, key, name = "auto-create-resource", uuid4().hex, f"auto-create-{uuid4().hex}"
    await db.resources.insert_one({
        "id": resource_id, "version": 1, "deletedAt": None, "kind": "HIKVISION_NETWORK",
        "ip": "192.0.2.60", "model": "verify-model", "subSerialNumber": "verify-serial",
        "authenticatedAt": now(),
    })
    body = {"name": name, "protocol": "SSH", "ip": "192.0.2.60", "port": 22,
            "resourceId": resource_id, "username": "root", "password": "temporary", "autoStart": True}
    created = await client.post("/api/v1/tasks", headers=headers(token) | {"Idempotency-Key": key}, json=body)
    check(created.status_code == 201, "自动启动创建没有返回 201")
    task = created.json()
    mapping = await db.idempotency.find_one({"actor": "bootstrap", "key": key})
    operation = await db.operations.find_one({"id": task["controlOperationId"]})
    audit_actions = {item["action"] async for item in db.audit.find({"targetId": task["id"]})}
    check(mapping and mapping["resourceId"] == task["id"] and mapping["state"] == "SUCCEEDED",
          "自动启动创建没有留下成功幂等映射")
    check((task["desiredState"], task["status"], task["nodeId"]) == ("RUNNING", "STOPPED", None),
          "自动启动初始状态不是可调度的 RUNNING/STOPPED")
    check(operation and (operation["action"], operation["desiredState"], operation["status"]) ==
          ("start", "RUNNING", "PENDING"), "自动启动没有留下唯一待完成 start 操作")
    check(audit_actions == {"create_task", "control:RUNNING"}, "自动启动创建审计不完整")

    replay = await client.post("/api/v1/tasks", headers=headers(token) | {"Idempotency-Key": key}, json=body)
    check(replay.status_code == 201 and replay.json()["id"] == task["id"], "同键自动启动创建没有复用任务")
    check(await db.tasks.count_documents({"name": name}) == 1 and
          await db.operations.count_documents({"taskId": task["id"]}) == 1,
          "同键自动启动创建产生了重复任务或操作")

    failed_resource, failed_key, failed_name = "auto-create-failed-resource", uuid4().hex, f"failed-{uuid4().hex}"
    await db.resources.insert_one({
        "id": failed_resource, "version": 1, "deletedAt": None, "kind": "HIKVISION_NETWORK",
        "ip": "192.0.2.61", "model": "verify-model", "subSerialNumber": "failed-serial",
        "authenticatedAt": now(),
    })
    before_resource = await db.resources.find_one({"id": failed_resource})
    before_counts = {name: await db[name].count_documents({}) for name in ("tasks", "operations", "idempotency", "audit")}
    original_audit = repo.audit

    async def failed_audit(*_args, **_kwargs):
        raise PyMongoError("injected auto-start creation audit failure")

    repo.audit = failed_audit
    try:
        failed = await client.post("/api/v1/tasks", headers=headers(token) | {"Idempotency-Key": failed_key}, json={
            "name": failed_name, "protocol": "SSH", "ip": "192.0.2.61", "port": 22,
            "resourceId": failed_resource, "username": "root", "password": "temporary", "autoStart": True,
        })
    finally:
        repo.audit = original_audit
    check(failed.status_code == 503, "自动启动创建审计失败没有返回 503")
    check(await db.resources.find_one({"id": failed_resource}) == before_resource,
          "自动启动创建失败改变了资源声明版本")
    for collection, count in before_counts.items():
        check(await db[collection].count_documents({}) == count, f"自动启动创建失败遗留 {collection} 写入")


async def main():
    """随机临时库和日志目录均在 finally 清理，不访问主库、Worker 或设备。"""
    configured, database_name, token = Settings(), f"task_control_tx_{uuid4().hex}", uuid4().hex
    check(database_name != configured.database_name, "验证脚本不能使用主数据库")
    temporary_logs = TemporaryDirectory(prefix="task-control-transactions-")
    try:
        settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=database_name,
                            bootstrap_token=token, encryption_key=Fernet.generate_key().decode(), admin_password="",
                            start_background=False, log_root=Path(temporary_logs.name) / "logs")
        mongo = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
        try:
            database, app = mongo[database_name], create_app(settings, mongo[database_name])
            async with app.router.lifespan_context(app):
                repo = app.state.repo
                transport = httpx.ASGITransport(app=app, client=("198.51.100.20", 45000), raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://verify") as client:
                    await verify_rollback(client, repo, database, token)
                    await verify_direct_cancellation(repo, database)
                    await verify_same_intent(client, database, token)
                    await verify_opposites(client, database, token)
                    await verify_paused_stop(client, repo, database, token)
                    await verify_resource_delete_race(app, client, database, token)
                    await verify_unknown_ack(client, database, token)
                    await verify_auto_start_creation(client, repo, database, token)
            result = {"passed": True, "realMongoReplicaSet": True, "formalFastApiRoutes": True,
                      "auditAndCancellationRollback": True, "concurrentControlReuse": True,
                      "oppositeIntentCancellation": True, "pausedStopRelease": True,
                      "resourceDeleteRace": True, "unknownCommitRecovery": True,
                      "autoStartCreationAtomic": True, "noWorkerOrDevice": True}
        finally:
            try:
                await mongo.drop_database(database_name)
            finally:
                await mongo.close()
    finally:
        temporary_logs.cleanup()
    print(json.dumps(result | {"temporaryDatabaseDropped": True, "temporaryLogDirectoryDropped": True}))


if __name__ == "__main__":
    asyncio.run(main())
