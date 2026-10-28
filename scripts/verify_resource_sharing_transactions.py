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


def index_names(plan):
    """递归提取获胜计划的索引名，兼容 OR 与排序合并的嵌套阶段。"""
    if isinstance(plan, dict):
        names = {plan["indexName"]} if "indexName" in plan else set()
        for value in plan.values():
            names.update(index_names(value))
        return names
    if isinstance(plan, list):
        return set().union(*(index_names(item) for item in plan)) if plan else set()
    return set()


def has_blocking_sort(plan):
    """SORT_MERGE 可流式合并两个有序索引分支，普通 SORT 则表示分页前内存排序。"""
    if isinstance(plan, dict):
        return plan.get("stage") == "SORT" or any(has_blocking_sort(value) for value in plan.values())
    if isinstance(plan, list):
        return any(has_blocking_sort(item) for item in plan)
    return False


async def verify_default_list_indexes(repo):
    """默认查看全部的当前与历史范围均须由创建时间索引稳定分页。"""
    evidence = {}
    for include_deleted, index_name in ((False, "resources_visible_created"), (True, "resources_created")):
        query = {} if include_deleted else {"deletedAt": None}
        explain = await repo.db.command("explain", {
            "find": "resources", "filter": query, "sort": {"createdAt": -1, "id": 1}, "limit": 20,
        }, verbosity="executionStats")
        plan = explain["queryPlanner"]["winningPlan"]
        stats = explain["executionStats"]
        check(index_name in index_names(plan), f"查看全部 includeDeleted={include_deleted} 未命中 {index_name}")
        check(not has_blocking_sort(plan), f"查看全部 includeDeleted={include_deleted} 出现阻塞排序")
        check(stats["nReturned"] == 20 and stats["totalDocsExamined"] <= 20,
              f"查看全部 includeDeleted={include_deleted} 首页扫描超出页面大小：{stats}")
        evidence[str(include_deleted).lower()] = {
            key: int(stats[key]) for key in ("nReturned", "totalKeysExamined", "totalDocsExamined")
        }
    return evidence


async def verify_mine_list_indexes(repo):
    """本人创建或被共享资源的 OR 分支在当前与历史范围都须索引排序。"""
    timestamp = now()
    await repo.db.resources.insert_many([
        {
            "id": f"mine-owned-{index}", "createdBy": "bob", "sharedUserIds": [], "deletedAt": None,
            "kind": "SERIAL_SERVER", "ip": f"192.0.2.{index}", "createdAt": timestamp,
        }
        for index in range(1, 31)
    ] + [
        {
            "id": f"mine-shared-{index}", "createdBy": "alice", "sharedUserIds": ["bob"], "deletedAt": None,
            "kind": "SERIAL_SERVER", "ip": f"192.0.3.{index}", "createdAt": timestamp,
        }
        for index in range(1, 31)
    ] + [
        {
            "id": f"mine-decoy-{index}", "createdBy": "other", "sharedUserIds": [], "deletedAt": None,
            "kind": "SERIAL_SERVER", "ip": f"192.0.4.{index}", "createdAt": timestamp,
        }
        for index in range(1, 401)
    ])
    evidence = {}
    for include_deleted, expected in (
        (False, {"resources_creator_visible_created", "resources_shared_visible_created"}),
        (True, {"resources_creator_created", "resources_shared_created"}),
    ):
        conditions = [{"$or": [{"createdBy": "bob"}, {"sharedUserIds": "bob"}]}]
        if not include_deleted:
            conditions.insert(0, {"deletedAt": None})
        query = conditions[0] if len(conditions) == 1 else {"$and": conditions}
        explain = await repo.db.command("explain", {
            "find": "resources", "filter": query, "sort": {"createdAt": -1, "id": 1}, "limit": 20,
        }, verbosity="executionStats")
        plan = explain["queryPlanner"]["winningPlan"]
        names = index_names(plan)
        stats = explain["executionStats"]
        check(expected.issubset(names),
              f"仅查看自己 includeDeleted={include_deleted} 未同时命中创建者和共享者的可分页索引：{names}")
        check(not has_blocking_sort(plan), f"仅查看自己 includeDeleted={include_deleted} 出现阻塞排序")
        # SORT_MERGE 为确定第20条后的全局顺序，需要从另一索引分支预读一个边界候选。
        check(stats["nReturned"] == 20 and stats["totalDocsExamined"] <= 21,
              f"仅查看自己 includeDeleted={include_deleted} 扫描超出双分支合并的一个边界候选：{stats}")
        evidence[str(include_deleted).lower()] = {
            key: int(stats[key]) for key in ("nReturned", "totalKeysExamined", "totalDocsExamined")
        }
    return evidence


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
        mine_explain = await verify_mine_list_indexes(repo)
        all_explain = await verify_default_list_indexes(repo)
        result = {"passed": True, "realMongoReplicaSet": True, "invalidShareUsersRejected": True,
                  "lowScopeNotElevated": True, "revokeControlRaceSerialized": True,
                  "controlWonBeforeRevoke": control_won, "resourceIdIndexUsed": True,
                  "resourceQuery": explain, "allListIndexesUsed": True, "allListQuery": all_explain,
                  "mineListIndexesUsed": True,
                  "mineListQuery": mine_explain, "noWorkerOrDevice": True}
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
