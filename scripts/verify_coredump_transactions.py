"""在独立真实 Mongo 库验证 coredump 配额事务和取消终态竞争，不访问设备。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.coredumps.jobs import _complete
from camera_logs.coredumps.jobs import _release as release_export
from camera_logs.coredumps.jobs import _reserve as reserve_export
from camera_logs.coredumps.snapshots import _release as release_snapshot
from camera_logs.coredumps.snapshots import _reserve as reserve_snapshot
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.errors import OperationFailure


async def verify_snapshot_claim_rollback(repo: Repository) -> None:
    """以 Mongo collection validator 注入 claim 写入失败，确认同事务总额没有残留。"""
    await repo.db.coredump_snapshot_claims.drop()
    await repo.db.create_collection(
        "coredump_snapshot_claims",
        validator={"$jsonSchema": {"bsonType": "object", "properties": {"bytes": {"maximum": 0}}}},
    )
    try:
        await reserve_snapshot(repo, "snapshot-rejected", "file", 8192)
    except OperationFailure as error:  # validator 拒绝 claim 是预期的事务失败注入。
        assert error.code == 121
    else:
        raise AssertionError("快照 claim validator 未拒绝写入")
    assert await repo.db.coredump_snapshot_claims.count_documents({}) == 0
    assert await repo.db.coredump_snapshot_reservations.count_documents({}) == 0
    await repo.db.coredump_snapshot_claims.drop()
    await repo.db.coredump_snapshot_claims.create_index("id", unique=True)

    await reserve_snapshot(repo, "snapshot", "file", 8192)
    aggregate = await repo.db.coredump_snapshot_reservations.find_one({"id": repo.settings.node_id})
    assert aggregate is not None and aggregate["used"] == 8192
    assert "snapshot" in aggregate["activeTokens"]
    await release_snapshot(repo, "snapshot")
    await release_snapshot(repo, "snapshot")
    aggregate = await repo.db.coredump_snapshot_reservations.find_one({"id": repo.settings.node_id})
    claim = await repo.db.coredump_snapshot_claims.find_one({"id": "snapshot"})
    assert aggregate["used"] == 0 and "snapshot" not in aggregate["activeTokens"]
    assert claim["state"] == "RELEASED"


def export_job(identifier: str, estimated_bytes: int = 60_000) -> dict:
    """构造只含计费输入的作业，不创建文件、不启动节点或访问设备。"""
    return {"id": identifier, "estimatedBytes": estimated_bytes, "sources": [{"id": "source"}]}


async def verify_export_reservations(repo: Repository) -> None:
    """并发预留不得超额；相同作业和重复释放均保持幂等。"""
    await repo.db.coredump_export_reservation_claims.create_index("id", unique=True)
    repo.settings.coredump_export_quota_bytes = 380_000
    jobs = [export_job(f"parallel-{index}") for index in range(3)]
    results = await asyncio.gather(*(reserve_export(repo, job) for job in jobs), return_exceptions=True)
    reserved = [result for result in results if isinstance(result, int)]
    assert len(reserved) == 2 and sum(reserved) <= repo.settings.coredump_export_quota_bytes
    total = await repo.db.coredump_export_reservations.find_one({"id": repo.settings.node_id})
    assert total["used"] == sum(reserved)
    successful = [job["id"] for job, result in zip(jobs, results) if isinstance(result, int)]
    assert all(await asyncio.gather(*(release_export(repo, identifier) for identifier in successful)))
    total = await repo.db.coredump_export_reservations.find_one({"id": repo.settings.node_id})
    assert total["used"] == 0

    shared = export_job("same-job", 20_000)
    repeated = await asyncio.gather(*(reserve_export(repo, shared) for _ in range(4)))
    assert len(set(repeated)) == 1
    total = await repo.db.coredump_export_reservations.find_one({"id": repo.settings.node_id})
    assert total["used"] == repeated[0]
    assert await release_export(repo, shared["id"]) is True
    assert await release_export(repo, shared["id"]) is False
    total = await repo.db.coredump_export_reservations.find_one({"id": repo.settings.node_id})
    assert total["used"] == 0


async def verify_cancel_wins_terminal_race(repo: Repository) -> None:
    """取消先完成时，成功终态 CAS 和成功审计都不能越过取消状态。"""
    job = {"id": "cancel-race", "actor": "verify", "workerInstanceId": "worker"}
    await repo.db.coredump_exports.insert_one({**job, "status": "RUNNING"})
    cancelled = asyncio.Event()

    async def cancel() -> None:
        changed = await repo.db.coredump_exports.update_one(
            {"id": job["id"], "status": "RUNNING"}, {"$set": {"status": "CANCELLED"}}
        )
        assert changed.modified_count == 1
        cancelled.set()

    async def complete_after_cancel() -> dict:
        await cancelled.wait()
        return await _complete(repo, job, {"status": "SUCCEEDED", "resultPath": "/not-created"})

    _unused, outcome = await asyncio.gather(cancel(), complete_after_cancel())
    assert outcome == {"status": "CANCELLED", "_transitioned": False}
    document = await repo.db.coredump_exports.find_one({"id": job["id"]})
    assert document["status"] == "CANCELLED"
    assert await repo.db.audit.count_documents({"action": "coredump_export_succeeded", "targetId": job["id"]}) == 0


async def main() -> None:
    """使用随机库和临时目录，任何断言或 Mongo 异常后都在 finally 中清理。"""
    configured, database_name = Settings(), "coredump_transactions_verify_" + uuid4().hex
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    temporary_path: Path | None = None
    try:
        with TemporaryDirectory(prefix="camera-coredump-transactions-") as temporary:
            temporary_path = Path(temporary)
            settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=database_name,
                                log_root=temporary_path / "logs", node_id="verify-node",
                                encryption_key=Fernet.generate_key().decode(), bootstrap_token="verify",
                                start_background=False)
            repo = Repository(mongo[database_name], settings)
            await repo.initialize()
            await verify_snapshot_claim_rollback(repo)
            await verify_export_reservations(repo)
            await verify_cancel_wins_terminal_race(repo)
            print(json.dumps({"passed": True, "snapshotClaimWriteRollback": True,
                              "snapshotReleaseIdempotent": True, "exportConcurrentReserve": True,
                              "exportReleaseIdempotent": True, "cancelWinsNoSuccessAudit": True,
                              "noDeviceAccess": True}))
    finally:
        await mongo.drop_database(database_name)
        assert database_name not in await mongo.list_database_names()
        await mongo.close()
    assert temporary_path is not None and not temporary_path.exists()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryDirectoryRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
