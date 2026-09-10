"""在隔离真实 Mongo 库验证同资源 Coredump 监控租约互斥，不访问设备。"""

import asyncio
import json
from datetime import timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


def online_resource(identifier: str) -> dict[str, object]:
    """生成最小在线资源；验证器只写随机库中的资源租约字段。"""
    return {"id": identifier, "kind": "HIKVISION_NETWORK", "ip": "192.0.2.200",
            "deletedAt": None, "healthStatus": "ONLINE"}


def collecting_task(identifier: str, resource_id: str) -> dict[str, object]:
    """生成最小 SSH 采集任务，满足运行归属与 Coredump 前置条件。"""
    return {"id": identifier, "resourceId": resource_id, "runId": f"{identifier}-run",
            "nodeId": "coredump-monitor-verify", "generation": 1, "protocol": "SSH",
            "desiredState": "RUNNING", "status": "COLLECTING", "resourceDeleted": False}


def guard_runtime(repo: Repository, task: dict[str, object]) -> SessionRuntime:
    """只构造 guard 所需内存状态，明确不创建采集器、后台协程或设备连接。"""
    runtime = object.__new__(SessionRuntime)
    runtime.repo, runtime.task = repo, dict(task)
    runtime.stopping = runtime.retired = False
    runtime.collector = SimpleNamespace(_closed=SimpleNamespace(is_set=lambda: False))
    runtime.background = None
    return runtime


async def assert_single_resource_winner(repo: Repository) -> None:
    """同资源两个正常采集任务并发抢占时，只允许一个运行获得租约。"""
    resource_id = "shared-resource"
    first, second = collecting_task("shared-a", resource_id), collecting_task("shared-b", resource_id)
    await repo.db.resources.insert_one(online_resource(resource_id))
    await repo.db.tasks.insert_many([first, second])
    outcomes = await asyncio.gather(*(guard_runtime(repo, task).coredump_guard() for task in (first, second)))
    assert outcomes.count(True) == 1 and outcomes.count(None) == 1, outcomes
    resource = await repo.db.resources.find_one({"id": resource_id})
    winner = first if outcomes[0] is True else second
    assert resource["coredumpLeaseTaskId"] == winner["id"]
    assert resource["coredumpLeaseRunId"] == winner["runId"]


async def assert_resources_are_independent(repo: Repository) -> None:
    """不同资源的租约写入独立，不能因为另一资源已领取而被拒绝。"""
    tasks = [collecting_task("independent-a", "resource-a"), collecting_task("independent-b", "resource-b")]
    await repo.db.resources.insert_many([online_resource("resource-a"), online_resource("resource-b")])
    await repo.db.tasks.insert_many(tasks)
    assert await asyncio.gather(*(guard_runtime(repo, task).coredump_guard() for task in tasks)) == [True, True]
    for task in tasks:
        resource = await repo.db.resources.find_one({"id": task["resourceId"]})
        assert resource["coredumpLeaseTaskId"] == task["id"]


async def assert_ineligible_tasks_cannot_claim(repo: Repository) -> None:
    """停止或失属的任务不能获取资源租约，也不能覆盖已有正常采集所有者。"""
    resource_id = "ineligible-resource"
    owner = collecting_task("eligible", resource_id)
    stopped = collecting_task("stopped", resource_id) | {"desiredState": "STOPPED", "status": "STOPPED"}
    stale = collecting_task("stale", resource_id)
    await repo.db.resources.insert_one(online_resource(resource_id))
    await repo.db.tasks.insert_many([owner, stopped, stale])
    assert await guard_runtime(repo, owner).coredump_guard() is True
    assert await guard_runtime(repo, stopped).coredump_guard() is False
    await repo.db.tasks.update_one({"id": stale["id"]}, {"$set": {"generation": 2}})
    assert await guard_runtime(repo, stale).coredump_guard() is False
    resource = await repo.db.resources.find_one({"id": resource_id})
    assert resource["coredumpLeaseTaskId"] == owner["id"]


async def assert_expiry_and_release_allow_same_resource_takeover(repo: Repository) -> None:
    """过期或明确释放后只能由同资源接管，其他资源的租约保持不变。"""
    resource_id, other_id = "takeover-resource", "unrelated-resource"
    first, second = collecting_task("takeover-a", resource_id), collecting_task("takeover-b", resource_id)
    other = collecting_task("unrelated", other_id)
    await repo.db.resources.insert_many([online_resource(resource_id), online_resource(other_id)])
    await repo.db.tasks.insert_many([first, second, other])
    first_runtime, second_runtime = guard_runtime(repo, first), guard_runtime(repo, second)
    assert await first_runtime.coredump_guard() is True
    assert await guard_runtime(repo, other).coredump_guard() is True
    await repo.db.resources.update_one({"id": resource_id}, {"$set": {"coredumpLeaseUntil": now() - timedelta(seconds=1)}})
    assert await second_runtime.coredump_guard() is True
    await repo.db.resources.update_one({"id": resource_id}, {"$unset": {
        "coredumpLeaseTaskId": "", "coredumpLeaseRunId": "", "coredumpLeaseGeneration": "",
        "coredumpLeaseNodeId": "", "coredumpLeaseUntil": "",
    }})
    assert await first_runtime.coredump_guard() is True
    shared, unrelated = await asyncio.gather(
        repo.db.resources.find_one({"id": resource_id}), repo.db.resources.find_one({"id": other_id}),
    )
    assert shared["coredumpLeaseTaskId"] == first["id"]
    assert unrelated["coredumpLeaseTaskId"] == other["id"]


async def main() -> None:
    """运行真实副本集 CAS 验证；finally 只删除本次随机数据库和临时目录。"""
    configured = Settings()
    database_name = f"coredump_monitor_verify_{uuid4().hex}"
    client = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                              w="majority", journal=True)
    try:
        with TemporaryDirectory(prefix="camera-coredump-monitor-") as directory:
            settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=database_name,
                                log_root=directory, encryption_key=Fernet.generate_key().decode(),
                                bootstrap_token="verify", start_background=False)
            repo = Repository(client[database_name], settings)
            await repo.initialize()
            await assert_single_resource_winner(repo)
            await assert_resources_are_independent(repo)
            await assert_ineligible_tasks_cannot_claim(repo)
            await assert_expiry_and_release_allow_same_resource_takeover(repo)
            print(json.dumps({"passed": True, "sameResourceSingleWinner": True,
                              "differentResourcesIndependent": True, "ineligibleRejected": True,
                              "expiredOrReleasedTakeover": True, "noDeviceAccess": True}))
    finally:
        await client.drop_database(database_name)
        assert database_name not in await client.list_database_names()
        await client.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryDirectoryRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
