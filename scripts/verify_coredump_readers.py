"""真实 Mongo 验证核心转储读者与到期清理互斥，只使用随机库和临时文件。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.coredumps.scanner import CoredumpScanner
from camera_logs.coredumps.snapshot_readers import SnapshotReader
from camera_logs.coredumps.snapshots import freeze, reconcile_snapshots, release_snapshot
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


async def verify_readers(repo: Repository, document: dict) -> dict:
    """多个消费者持有同一固定版本，清理必须等最后一个消费者退出。"""
    frozen = await freeze(repo, document)
    path = Path(frozen["snapshot"]["path"])
    remote = Repository(repo.db, repo.settings.model_copy(update={"node_id": "other-coordinator"}))
    readers = [SnapshotReader(repo if index < 6 else remote, frozen) for index in range(12)]
    delegated = None
    try:
        await asyncio.gather(*(reader.acquire() for reader in readers))
        await release_snapshot(repo, frozen)
        assert path.is_file(), "活动读者期间不得删除快照"
        current = await repo.db.coredump_files.find_one({"id": document["id"]})
        assert current["status"] == "RETIRING"
        late = SnapshotReader(repo, frozen)
        try:
            await late.acquire()
        except (FileNotFoundError, RuntimeError):
            pass
        else:
            await late.close()
            raise AssertionError("清理开始后不能接受新读者")
        delegated = SnapshotReader(repo, frozen, parent_id=readers[-1].identifier)
        await delegated.acquire()
        await asyncio.gather(*(reader.close() for reader in readers[:-1]))
        await reconcile_snapshots(repo)
        assert path.is_file(), "最后一个读者仍存在时不得清理"
    finally:
        await asyncio.gather(*(reader.close() for reader in readers))
        if delegated is not None:
            try:
                assert await delegated._renew_once(), "已获授权的子读者应能独立续租"
                await reconcile_snapshots(repo)
                assert path.is_file(), "父读者关闭后仍须保留已获授权的子读者"
            finally:
                await delegated.close()
    await reconcile_snapshots(repo)
    current = await repo.db.coredump_files.find_one({"id": document["id"]})
    assert current["status"] == "RECEIVING" and not path.exists()
    aggregate = await repo.db.coredump_snapshot_reservations.find_one({"id": repo.settings.node_id})
    assert aggregate["used"] == 0
    assert not current.get("snapshotReaders")
    return current


async def verify_competing_acquisition(repo: Repository, document: dict) -> None:
    """使用真实Mongo并发CAS，允许读者或清理先赢，但不允许已获租约读者失去文件。"""
    for _ in range(12):
        current = await repo.db.coredump_files.find_one({"id": document["id"]})
        frozen = await freeze(repo, current)
        path = Path(frozen["snapshot"]["path"])
        reader = SnapshotReader(repo, frozen)
        try:
            result, released = await asyncio.gather(
                reader.acquire(), release_snapshot(repo, frozen), return_exceptions=True,
            )
            assert not isinstance(released, BaseException), repr(released)
            if isinstance(result, BaseException):
                assert isinstance(result, (FileNotFoundError, RuntimeError)), type(result).__name__
            else:
                assert path.is_file(), "获取读者租约后快照被并发清理"
        finally:
            await reader.close()
        await reconcile_snapshots(repo)


async def main() -> None:
    """初始化随机副本集库，最终无论成功失败都清理自建库与文件。"""
    configured = Settings()
    database_name = "coredump_readers_verify_" + uuid4().hex
    client = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                              w="majority", journal=True)
    try:
        with TemporaryDirectory(prefix="camera-coredump-readers-") as directory:
            root = Path(directory)
            settings = Settings(_env_file=None, log_root=root / "logs", nfs_root=root / "nfs",
                                node_id="verify-node", encryption_key=Fernet.generate_key().decode())
            source_dir = settings.nfs_root / "192.0.2.88"
            source_dir.mkdir(parents=True)
            source = source_dir / "core.bin"
            source.write_bytes(b"immutable coredump\n" * 1024)
            repo = Repository(client[database_name], settings)
            await repo.initialize()
            await repo.db.resources.insert_one({"id": "verify-resource", "ip": "192.0.2.88",
                                                "kind": "HIKVISION_NETWORK", "deletedAt": None})
            await CoredumpScanner(repo).scan_once()
            document = await repo.db.coredump_files.find_one({"resourceId": "verify-resource"})
            current = await verify_readers(repo, document)
            await verify_competing_acquisition(repo, current)
            assert source.is_file(), "快照维护不能删除NFS源文件"
            print(json.dumps({"passed": True, "parallelReaders": 12, "acquisitionRaces": 12,
                              "sourceRetained": True}))
    finally:
        await client.drop_database(database_name)
        assert database_name not in await client.list_database_names()
        await client.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryDirectoryRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
