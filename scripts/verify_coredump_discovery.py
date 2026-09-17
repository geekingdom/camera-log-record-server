"""用真实本机 Mongo 验证 Coredump 存在筛选、幂等事件及关联查询计划。"""

import asyncio
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.coredumps.presence import page_resources_by_presence, resource_presence_pipeline
from camera_logs.coredumps.scanner import CoredumpScanner
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from verify_isolated_browser_smoke import local_mongo_uri


def lookup_stats(value):
    """抽取 Explain 的关联索引与扫描量，不输出任何业务正文或凭据。"""
    if isinstance(value, dict):
        if "$lookup" in value and "indexesUsed" in value:
            return {key: value.get(key) for key in (
                "indexesUsed", "totalDocsExamined", "totalKeysExamined", "collectionScans", "nReturned",
            )}
        for child in value.values():
            found = lookup_stats(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = lookup_stats(child)
            if found:
                return found
    return {}


async def main():
    """仅在随机数据库及临时文件夹中运行验证，结束必定删除实验数据。"""
    configured = Settings()
    if not local_mongo_uri(configured.mongo_uri):
        raise RuntimeError("仅允许本机 Mongo 验证")
    database = f"coredump_discovery_{uuid4().hex}"
    mongo = AsyncMongoClient(configured.mongo_uri, tz_aware=True)
    try:
        with tempfile.TemporaryDirectory(prefix="camera-core-discovery-") as temporary:
            root = Path(temporary)
            settings = configured.model_copy(update={"node_id": "discovery-test", "nfs_root": root,
                                                      "encryption_key": Fernet.generate_key().decode()})
            repo = Repository(mongo[database], settings)
            await repo.initialize()
            resource = {"id": "camera", "name": "验证设备", "kind": "HIKVISION_NETWORK",
                        "ip": "192.0.2.35", "deletedAt": None, "createdAt": now()}
            await repo.db.resources.insert_one(resource)
            directory = root / resource["ip"]
            directory.mkdir()
            source = directory / "probe.core"
            source.write_bytes(b"synthetic coredump")
            scanner = CoredumpScanner(repo)
            await scanner.scan_once()
            await scanner.scan_once()
            assert await repo.db.events.count_documents({"type": "COREDUMP_DISCOVERED"}) == 1
            present = await page_resources_by_presence(repo.db, {"deletedAt": None}, 1, 20, True)
            assert present["total"] == 1 and present["items"][0]["id"] == "camera"
            source.unlink()
            await scanner.scan_once()
            absent = await page_resources_by_presence(repo.db, {"deletedAt": None}, 1, 20, False)
            assert absent["total"] == 1
            await repo.db.resources.insert_many([
                {"id": f"resource-{i:03}", "createdAt": now(), "deletedAt": None} for i in range(100)
            ])
            await repo.db.coredump_files.insert_many([
                {"id": f"file-{i}", "resourceId": f"resource-{i % 50:03}", "name": f"core-{i}",
                 "receivedAt": now(), "nodeId": "discovery-test", "sourcePresent": True} for i in range(1000)
            ])
            for exists, total in ((True, 50), (False, 51)):
                first = await page_resources_by_presence(repo.db, {"deletedAt": None}, 1, 20, exists)
                second = await page_resources_by_presence(repo.db, {"deletedAt": None}, 2, 20, exists)
                assert first["total"] == second["total"] == total
                assert len(first["items"]) == len(second["items"]) == 20
                assert not ({r["id"] for r in first["items"]} & {r["id"] for r in second["items"]})
            explain = await repo.db.command("explain", {
                "aggregate": "resources", "pipeline": resource_presence_pipeline({"deletedAt": None}, 1, 20, True),
                "cursor": {},
            }, verbosity="executionStats")
            stats = lookup_stats(explain)
            assert stats.get("indexesUsed") and stats.get("collectionScans") == 0, stats
            probe = await repo.db.command("explain", {
                "find": "coredump_files", "filter": {"nodeId": "discovery-test", "id": {"$gt": "file-500"},
                                                        "sourcePresent": {"$ne": False}},
                "sort": {"id": 1}, "limit": 50,
            }, verbosity="executionStats")
            plan = json.dumps(probe["queryPlanner"]["winningPlan"], default=str)
            assert "nodeId_1_id_1" in plan and '"stage": "SORT"' not in plan, plan
            probe_stats = {key: probe["executionStats"][key]
                           for key in ("nReturned", "totalKeysExamined", "totalDocsExamined")}
            print(json.dumps({"passed": True, "lookup": stats, "discoveryDeduplicated": True,
                              "missingSourceCleared": True, "presencePagination": True, "probe": probe_stats}))
    finally:
        await mongo.drop_database(database)
        await mongo.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryFilesDropped": True}))


if __name__ == "__main__":
    asyncio.run(main())
