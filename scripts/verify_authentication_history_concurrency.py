"""在真实 MongoDB 副本集验证认证历史并发聚合和失败边界。

脚本只创建随机临时数据库，结束后无条件删除。MongoDB 单机不支持事务，因此会
先验证副本集能力并拒绝在不具备事务语义的目标上给出错误结论。
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.resources.authentication_records import record_authentication
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


async def _write_concurrent_result(repo, resource: dict, result: str, completed_at: datetime) -> list[str]:
    """以不同消息并发写入同一结果，验证消息不会成为周期记录的分段条件。"""
    messages = [f"{result.lower()}-attempt-{index}" for index in range(16)]
    await asyncio.gather(*(
        record_authentication(
            repo, resource, source="PERIODIC", result=result, before=resource, after=resource,
            message=message, completed_at=completed_at,
        )
        for message in messages
    ))
    return messages


async def _assert_concurrent_result_buckets(repo, resource: dict, started: datetime) -> list[tuple[str, int]]:
    """断言四种周期结果各自在 16 路并发后压缩成一段，且不延长首条 TTL。"""
    results = ("SUCCESS", "AUTH_FAILED", "OFFLINE", "ERROR")
    for offset, result in enumerate(results):
        completed_at = started + timedelta(minutes=offset)
        messages = await _write_concurrent_result(repo, resource, result, completed_at)
        rows = [row async for row in repo.db.authentication_records.find(
            {"resourceId": resource["id"], "result": result},
        )]
        if len(rows) != 1:
            raise AssertionError(f"{result} 并发写入未聚合为一段 rows={rows!r}")
        row = rows[0]
        if row.get("occurrenceCount") != 16:
            raise AssertionError(f"{result} 并发计数不守恒 row={row!r}")
        if row.get("message") not in messages:
            raise AssertionError(f"{result} 聚合后未保留本批消息 row={row!r}")
    rows = [row async for row in repo.db.authentication_records.find(
        {"resourceId": resource["id"]},
    ).sort("createdAt", 1)]
    actual = [(row["result"], row.get("occurrenceCount")) for row in rows]
    expected = [(result, 16) for result in results]
    if actual != expected:
        raise AssertionError(f"并发各结果的分段或计数不符合预期 actual={actual!r}")
    return actual


async def _assert_message_merge_and_fixed_ttl(repo, resource: dict, started: datetime) -> None:
    """断言不同消息仍合并，并保留首条记录的绝对过期时间。"""
    for offset, result in enumerate(("SUCCESS", "AUTH_FAILED", "OFFLINE", "ERROR")):
        completed_at = started + timedelta(minutes=offset)
        message_resource = resource | {"id": f"message-camera-{result.lower()}"}
        await record_authentication(
            repo, message_resource, source="PERIODIC", result=result,
            before=message_resource, after=message_resource, message="earlier-message", completed_at=completed_at,
        )
        first = await repo.db.authentication_records.find_one({"resourceId": message_resource["id"]})
        await record_authentication(
            repo, message_resource, source="PERIODIC", result=result,
            before=message_resource, after=message_resource, message="latest-message",
            completed_at=completed_at + timedelta(seconds=1),
        )
        refreshed = await repo.db.authentication_records.find_one({"_id": first["_id"]})
        if (refreshed.get("occurrenceCount") != 2 or refreshed.get("message") != "latest-message"):
            raise AssertionError(f"{result} 的不同消息未合并或最近消息未更新 row={refreshed!r}")
        if refreshed.get("expiresAt") != first.get("expiresAt"):
            raise AssertionError(f"{result} 聚合延长了固定 TTL row={refreshed!r}")


async def _assert_alternating_result_boundaries(repo, resource: dict, started: datetime) -> list[tuple[str, int]]:
    """断言成功与认证失败交错时，连续段的次数守恒且不能跨越状态边界。"""
    sequence = (("SUCCESS", 3), ("AUTH_FAILED", 2), ("SUCCESS", 4), ("AUTH_FAILED", 3))
    for offset, (result, count) in enumerate(sequence):
        for index in range(count):
            await record_authentication(
                repo, resource, source="PERIODIC", result=result, before=resource, after=resource,
                message=f"{result.lower()}-segment-{offset}-attempt-{index}",
                completed_at=started + timedelta(minutes=offset, seconds=index),
            )
    rows = [row async for row in repo.db.authentication_records.find(
        {"resourceId": resource["id"]},
    ).sort("historyFirstRevision", 1)]
    actual = [(row["result"], row.get("occurrenceCount")) for row in rows]
    if actual != list(sequence):
        raise AssertionError(f"成功和失败交错后分段或次数不守恒 actual={actual!r}")
    if any(
        row["historyFirstRevision"] < next_row["historyFirstRevision"] < row["historyLatestRevision"]
        for row, next_row in pairwise(rows)
    ):
        raise AssertionError(f"认证历史段跨越了后续状态边界 actual={actual!r}")
    return actual


async def verify(uri: str) -> dict[str, object]:
    """在真实副本集验证所有周期结果的连续聚合、状态边界和固定 TTL。"""
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000)
    # MongoDB 数据库名最多63字符；保持短前缀也便于人工识别并清理失败残留。
    database_name = f"cl_auth_{uuid.uuid4().hex}"
    try:
        hello = await client.admin.command("hello")
        if not hello.get("setName"):
            raise RuntimeError("认证历史并发验证需要 MongoDB 副本集事务")
        settings = Settings(
            _env_file=None, mongo_uri=uri, database_name=database_name,
            encryption_key=Fernet.generate_key().decode(), authentication_record_retention_days=90,
        )
        repo = Repository(client[database_name], settings)
        await repo.initialize()
        resource = {"id": "concurrent-camera", "authenticatedAt": datetime(2026, 9, 11, tzinfo=UTC),
                    "model": "DS-TEST", "subSerialNumber": "SERIAL-1"}
        started = datetime(2026, 9, 11, 8, tzinfo=UTC)
        concurrent_records = await _assert_concurrent_result_buckets(repo, resource, started)
        await _assert_message_merge_and_fixed_ttl(repo, resource, started + timedelta(minutes=10))
        boundary_resource = resource | {"id": "alternating-camera"}
        boundary_records = await _assert_alternating_result_boundaries(
            repo, boundary_resource, started + timedelta(hours=1),
        )
        head = await repo.db.authentication_record_heads.find_one({"_id": boundary_resource["id"]})
        latest = await repo.db.authentication_records.find_one({"id": head.get("latestRecordId")}) if head else None
        if not head or not latest or latest.get("resourceId") != boundary_resource["id"]:
            raise AssertionError("认证历史 head 未指向事务序列最后一条记录")
        return {"passed": True, "temporaryDatabase": database_name,
                "concurrentRecords": concurrent_records, "boundaryRecords": boundary_records,
                "headRevision": head["revision"]}
    finally:
        await client.drop_database(database_name)
        await client.close()


if __name__ == "__main__":
    import os

    value = asyncio.run(verify(os.environ.get("MONGO_URI", Settings().mongo_uri)))
    print(value)
