"""在随机专用 Mongo 库验证搜索中间结果发布的租约 CAS 与取消竞争。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.logs.search_results import publish_search_results
from pymongo import AsyncMongoClient


def result(marker: str) -> list[dict[str, object]]:
    """构造可比较的最小搜索结果前缀，确保失败发布不会隐式修改结果。"""
    return [{"fileId": "file", "offset": 0, "text": marker}]


def running_job(identifier: str, token: str = "current-token", *, lease: datetime | None = None) -> dict:
    """生成带执行归属的 RUNNING 作业，发布时间必须匹配 token、节点和未过期租约。"""
    return {
        "id": identifier,
        "kind": "SEARCH",
        "status": "RUNNING",
        "nodeId": "verify-node",
        "executionToken": token,
        "leaseUntil": lease or datetime.now(UTC) + timedelta(minutes=5),
        "results": result("baseline"),
        "resultCount": 1,
    }


async def assert_rejected(repo, job: dict, expected: dict) -> None:
    """确认不匹配归属的发布返回 false，且既有结果文档逐字段保持不变。"""
    assert not await publish_search_results(repo, job, result("rejected"))
    stored = await repo.db.jobs.find_one({"id": job["id"]})
    assert stored["status"] == expected["status"]
    assert stored.get("results") == expected.get("results")
    assert stored.get("resultCount") == expected.get("resultCount")


async def main() -> None:
    """只写入并删除本脚本的随机库，不访问平台业务库或设备日志。"""
    settings = Settings()
    database = f"verify_search_publication_{uuid4().hex}"
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    repo = SimpleNamespace(db=client[database])
    try:
        valid = running_job("valid")
        await repo.db.jobs.insert_one(valid)
        published = result("published")
        assert await publish_search_results(repo, valid, published)
        stored = await repo.db.jobs.find_one({"id": "valid"})
        assert stored["status"] == "RUNNING" and stored["results"] == published and stored["resultCount"] == 1

        current = running_job("old-token")
        await repo.db.jobs.insert_one(current)
        await assert_rejected(repo, current | {"executionToken": "old-token"}, current)

        expired = running_job("expired", lease=datetime.now(UTC) - timedelta(seconds=1))
        await repo.db.jobs.insert_one(expired)
        await assert_rejected(repo, expired, expired)

        cancelled = running_job("cancelled") | {"status": "CANCELLED"}
        await repo.db.jobs.insert_one(cancelled)
        await assert_rejected(repo, cancelled, cancelled)

        for index in range(20):
            job = running_job(f"race-{index}")
            await repo.db.jobs.insert_one(job)
            published = result(f"race-{index}")
            publish, cancel = await asyncio.gather(
                publish_search_results(repo, job, published),
                repo.db.jobs.update_one({"id": job["id"], "status": "RUNNING"}, {"$set": {"status": "CANCELLED"}}),
            )
            stored = await repo.db.jobs.find_one({"id": job["id"]})
            assert stored["status"] == "CANCELLED"
            # 若发布先胜出，取消只转换终态而保留已确认前缀；取消先胜出则基线保持。
            assert stored["results"] in (job["results"], published)
            assert stored["resultCount"] == 1
            assert not await publish_search_results(repo, job, result(f"late-{index}"))
            after_late = await repo.db.jobs.find_one({"id": job["id"]})
            assert after_late["status"] == "CANCELLED" and after_late["results"] == stored["results"]
            assert publish or cancel.modified_count == 1
    finally:
        await client.drop_database(database)
        assert database not in await client.list_database_names()
        await client.close()
    print(json.dumps({"validLeasePublish": True, "staleAndTerminalRejected": True,
                      "publishCancelRaces": 20, "temporaryDatabaseRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
