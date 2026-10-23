"""在真实 Mongo 副本集验证 Coredump 扫描栅栏与资源重添的并发收敛。"""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common import audited_mutations
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.coredumps.resource_identity import reassociate_coredump_history, stable_source_key
from camera_logs.coredumps.scanner import CoredumpScanner
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


def resource(identifier: str, ip: str, *, deleted_at=None) -> dict:
    """构造完整认证身份，验证只会在三元身份全部一致时迁移历史目录。"""
    return {
        "id": identifier,
        "kind": "HIKVISION_NETWORK",
        "ip": ip,
        "model": "DS-2CD-VERIFY",
        "subSerialNumber": "SN-VERIFY",
        "deletedAt": deleted_at,
    }


def catalog(identifier: str, owner: dict, node_id: str) -> dict:
    """构造不访问 NFS 的扫描目录事实，键与扫描器稳定身份计算完全相同。"""
    source = {"device": 7, "inode": 11, "size": 4, "mtimeNs": 1, "ctimeNs": 1}
    return {
        "id": identifier,
        "nodeId": node_id,
        "resourceId": owner["id"],
        "deviceIp": owner["ip"],
        "name": "historic.core",
        "sourceKey": stable_source_key(node_id, owner, "historic.core", 7, 11),
        "source": source,
        "version": 1,
        "status": "RECEIVING",
        "receivedAt": datetime.now(UTC),
        "firstSeenAt": datetime.now(UTC),
        "sourcePresent": True,
        "sourceState": "OBSERVING",
    }


async def soft_delete(repo: Repository, identifier: str, start: asyncio.Event) -> None:
    """与扫描器同时写资源行，真实事务必须和扫描栅栏产生写冲突并重试。"""
    await start.wait()

    async def commit(session):
        changed = await repo.db.resources.update_one(
            {"id": identifier, "deletedAt": None},
            {"$set": {"deletedAt": datetime.now(UTC)}}, session=session,
        )
        if changed.matched_count != 1:
            raise AssertionError("并发软删除未命中活动资源")

    await audited_mutations.mutation_transaction(repo, commit)


async def verify_one_race(repo: Repository, number: int) -> None:
    """验证任一提交顺序最终都没有旧资源归属或稳定键重复目录。"""
    old = resource(f"old-{number}", f"198.51.100.{number + 1}")
    await repo.db.resources.insert_one(old)
    scanner = CoredumpScanner(repo)
    initial = catalog(f"core-{number}", old, repo.settings.node_id)
    start = asyncio.Event()

    async def scan_old():
        await start.wait()
        return await scanner._insert_catalog_with_resource_fence(
            old, initial, initial["sourceKey"], initial["sourceKey"]
        )

    old_scan = asyncio.create_task(scan_old())
    deletion = asyncio.create_task(soft_delete(repo, old["id"], start))
    start.set()
    await asyncio.gather(old_scan, deletion)

    new = resource(f"new-{number}", old["ip"])

    async def recreate(session):
        await repo.db.resources.insert_one(new, session=session)
        await reassociate_coredump_history(repo, new, session=session)

    await audited_mutations.mutation_transaction(repo, recreate)
    current = catalog(f"new-core-{number}", new, repo.settings.node_id)
    catalog_row, _inserted = await scanner._insert_catalog_with_resource_fence(
        new, current, current["sourceKey"], current["sourceKey"]
    )
    assert catalog_row is not None
    rows = [row async for row in repo.db.coredump_files.find({"deviceIp": old["ip"]})]
    if len(rows) != 1 or rows[0]["resourceId"] != new["id"]:
        raise AssertionError(f"重添并发后目录归属未收敛 rows={rows!r}")
    if await repo.db.coredump_files.count_documents({"resourceId": old["id"]}) != 0:
        raise AssertionError("软删资源仍残留 Coredump 归属")


async def main() -> None:
    """只操作随机数据库和临时目录，任何成功或失败分支均清理实验产物。"""
    configured = Settings()
    uri = os.environ.get("MONGO_URI", configured.mongo_uri)
    database_name = f"coredump_reassociation_verify_{uuid4().hex}"
    mongo = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    temporary_path: Path | None = None
    try:
        with TemporaryDirectory(prefix="camera-coredump-reassociation-") as temporary:
            temporary_path = Path(temporary)
            settings = Settings(
                _env_file=None, mongo_uri=uri, database_name=database_name,
                encryption_key=Fernet.generate_key().decode(), log_root=temporary_path / "logs",
                nfs_root=temporary_path / "nfs", node_id="reassociation-verify", start_background=False,
            )
            repo = Repository(mongo[database_name], settings)
            await repo.initialize()
            for number in range(24):
                await verify_one_race(repo, number)
            print(json.dumps({"passed": True, "realMongoTransactions": True,
                              "concurrentSoftDeleteAndScan": 24, "readdHistoryReassociated": True,
                              "noOldResourceCatalog": True, "noStableKeyDuplicates": True}))
    finally:
        await mongo.drop_database(database_name)
        assert database_name not in await mongo.list_database_names()
        await mongo.close()
    assert temporary_path is not None and not temporary_path.exists()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryDirectoryRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
