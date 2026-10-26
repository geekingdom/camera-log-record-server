"""以真实 MongoDB 副本集验证资源共享的任务控制授权与撤销竞争。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common import audited_mutations
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.resources.sharing import validate_share_users
from camera_logs.tasks.control import request_control
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pymongo import AsyncMongoClient


def check(condition, message):
    """用中文说明验证不变量，输出中不包含临时连接配置或设备凭据。"""
    if not condition:
        raise AssertionError(message)


async def seed(repo):
    """创建一个所有者为 alice、仅向 bob 共享的已停止任务，不连接任何设备。"""
    timestamp = now()
    await repo.db.users.insert_many([
        {"id": "alice", "username": "alice", "enabled": True, "deletedAt": None},
        {"id": "bob", "username": "bob", "enabled": True, "deletedAt": None},
        {"id": "disabled", "username": "disabled", "enabled": False, "deletedAt": None},
    ])
    await repo.db.resources.insert_one({
        "id": "resource", "createdBy": "alice", "sharedUserIds": ["bob"], "deletedAt": None,
        "kind": "HIKVISION_NETWORK", "ip": "192.0.2.210", "version": 1, "createdAt": timestamp,
    })
    await repo.db.tasks.insert_one({
        "id": "task", "createdBy": "alice", "resourceId": "resource", "protocol": "SSH",
        "ip": "192.0.2.210", "port": 22, "desiredState": "STOPPED", "status": "STOPPED",
        "nodeId": None, "generation": 1, "resourceDeleted": False, "deletedAt": None,
        "createdAt": timestamp, "updatedAt": timestamp,
    })


def identity(identifier, scopes):
    """构造与正式认证结果相同形状的非管理员身份，明确保留平台作用域检查。"""
    return {"id": identifier, "isAdmin": False, "scopes": scopes}


async def revoke(repo):
    """在独立事务中撤销 bob，和任务控制对同一资源声明形成写冲突。"""
    async def commit(session):
        changed = await repo.db.resources.update_one(
            {"id": "resource", "deletedAt": None, "sharedUserIds": "bob"},
            {"$set": {"sharedUserIds": []}, "$inc": {"controlClaimVersion": 1}}, session=session,
        )
        check(changed.matched_count == 1, "撤销共享时未找到 bob 的有效共享资源")

    await audited_mutations.mutation_transaction(repo, commit)


async def expect_forbidden(coroutine, message):
    """拒绝的操作不得把权限错误转换成局部成功。"""
    try:
        await coroutine
    except HTTPException as error:
        check(error.status_code == 403, message)
    else:
        raise AssertionError(message)


async def verify_invalid_users(repo):
    """共享名单只接受启用且未删除的用户，未知或禁用用户都不能进入名单。"""
    for identifier in ("missing", "disabled"):
        try:
            await validate_share_users(repo, [identifier])
        except HTTPException as error:
            check(error.status_code == 422, "无效共享用户没有返回 422")
        else:
            raise AssertionError("无效共享用户被接受")


async def verify_scope_boundary(repo):
    """共享资源只补对象资格，不能补发 tasks:control 平台权限。"""
    before = await repo.db.operations.count_documents({"taskId": "task"})
    await expect_forbidden(
        request_control(repo, "task", "STOPPED", identity("bob", ["tasks:read"])),
        "低权限共享用户获得了任务控制资格",
    )
    check(await repo.db.operations.count_documents({"taskId": "task"}) == before,
          "低权限共享用户被拒绝后仍写入控制操作")


async def verify_revoke_race(repo):
    """控制和撤销可按任一事务顺序提交；撤销后不得再接受协作者控制。"""
    start = asyncio.Event()

    async def control():
        await start.wait()
        try:
            return await request_control(repo, "task", "STOPPED", identity("bob", ["tasks:control"]))
        except HTTPException as error:
            return error

    async def remove():
        await start.wait()
        await revoke(repo)

    control_task = asyncio.create_task(control())
    revoke_task = asyncio.create_task(remove())
    start.set()
    result, _ = await asyncio.gather(control_task, revoke_task)
    check(not isinstance(result, HTTPException) or result.status_code == 403,
          "控制与撤销竞争返回了非授权错误")
    resource = await repo.db.resources.find_one({"id": "resource"})
    check(resource["sharedUserIds"] == [], "共享撤销提交后资源仍保留协作者")
    before = await repo.db.operations.count_documents({"taskId": "task"})
    await expect_forbidden(
        request_control(repo, "task", "STOPPED", identity("bob", ["tasks:control"])),
        "撤销共享后协作者仍可控制任务",
    )
    check(await repo.db.operations.count_documents({"taskId": "task"}) == before,
          "撤销后的拒绝控制仍写入了操作")
    return not isinstance(result, HTTPException)


async def verify_query_index(repo):
    """共享资格按唯一资源 ID 读取；共享数组筛选只检查这一份资源，不新增写放大索引。"""
    explain = await repo.db.command("explain", {
        "find": "resources", "filter": {"id": "resource", "deletedAt": None, "sharedUserIds": "bob"},
    }, verbosity="executionStats")
    plan = json.dumps(explain["queryPlanner"]["winningPlan"], default=str)
    stats = explain["executionStats"]
    check("id_1" in plan and stats["totalDocsExamined"] <= 1,
          "共享资格查询未使用资源 ID 唯一索引限定候选")
    return {key: stats[key] for key in ("nReturned", "totalKeysExamined", "totalDocsExamined")}


async def main():
    """随机数据库和临时日志目录总在 finally 清理，要求配置指向可事务提交的副本集。"""
    configured = Settings()
    database_name = "resource_sharing_tx_" + uuid4().hex
    check(database_name != configured.database_name, "验证脚本不能使用主数据库")
    logs = TemporaryDirectory(prefix="resource-sharing-transactions-")
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                             w="majority", journal=True)
    database_initialized = False
    try:
        settings = configured.model_copy(update={"database_name": database_name,
                                                   "encryption_key": Fernet.generate_key().decode(),
                                                   "log_root": Path(logs.name) / "logs"})
        repo = Repository(mongo[database_name], settings)
        await repo.initialize()
        database_initialized = True
        await seed(repo)
        await verify_invalid_users(repo)
        await verify_scope_boundary(repo)
        control_won = await verify_revoke_race(repo)
        explain = await verify_query_index(repo)
        result = {"passed": True, "realMongoReplicaSet": True, "invalidShareUsersRejected": True,
                  "lowScopeNotElevated": True, "revokeControlRaceSerialized": True,
                  "controlWonBeforeRevoke": control_won, "resourceIdIndexUsed": True,
                  "resourceQuery": explain, "noWorkerOrDevice": True}
    finally:
        try:
            if database_initialized:
                await mongo.drop_database(database_name)
        finally:
            await mongo.close()
            logs.cleanup()
    print(json.dumps(result | {"temporaryDatabaseDropped": True, "temporaryLogDirectoryDropped": True}))


if __name__ == "__main__":
    asyncio.run(main())
