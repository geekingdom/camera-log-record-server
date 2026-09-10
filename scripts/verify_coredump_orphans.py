"""真实副本集验证孤立快照回收，所有文件和数据库仅属于本次隔离实验。"""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.coredumps.safety import snapshots_root
from camera_logs.coredumps.scanner import CoredumpScanner
from camera_logs.coredumps.snapshots import _reserve, freeze, reconcile_snapshots
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


async def expire(repo: Repository, token: str) -> None:
    """只缩短本次实验声明的到期时间，不修改服务保留配置。"""
    await repo.db.coredump_snapshot_claims.update_one(
        {"id": token}, {"$set": {"expiresAt": now() - timedelta(seconds=1)}},
    )


async def verify_candidate_index(repo: Repository) -> int:
    """确认大量已释放历史声明不会令到期候选查询读取整个集合。"""
    await repo.db.coredump_snapshot_claims.insert_many([
        {"id": f"released-history-{index}", "nodeId": repo.settings.node_id,
         "state": "RELEASED", "expiresAt": now() - timedelta(days=1)}
        for index in range(1000)
    ])
    query = {"nodeId": repo.settings.node_id, "$or": [
        {"state": "RECLAIMING"},
        {"state": {"$in": ["RESERVED", "PUBLISHED"]}, "expiresAt": {"$lte": now()}},
    ]}
    result = await repo.db.command(
        "explain", {"find": "coredump_snapshot_claims", "filter": query}, verbosity="executionStats",
    )
    examined = result["executionStats"]["totalDocsExamined"]
    assert examined <= 5, f"到期查询读取了过多历史声明：{examined}"
    return examined


async def verify(repo: Repository) -> None:
    """复现旧清理中断后的已发布孤儿，以及未发布的临时/最终副本遗留。"""
    device_ip = "192.0.2.89"
    source_dir = repo.settings.nfs_root / device_ip
    source_dir.mkdir(parents=True)
    source = source_dir / "core.bin"
    payload = b"orphan recovery verification\n"
    source.write_bytes(payload)
    await repo.db.resources.insert_one({"id": "verify-resource", "ip": device_ip,
                                       "kind": "HIKVISION_NETWORK", "deletedAt": None})
    await CoredumpScanner(repo).scan_once()
    original = await repo.db.coredump_files.find_one({"resourceId": "verify-resource"})
    frozen = await freeze(repo, original)
    published_token = frozen["snapshot"]["reservationToken"]
    published_path = Path(frozen["snapshot"]["path"])
    await expire(repo, published_token)

    # 模拟旧版本清理在删除文件前清除catalog引用的中断窗口。
    await repo.db.coredump_files.update_one(
        {"id": original["id"]}, {"$set": {"status": "RECEIVING"}, "$unset": {"snapshot": ""}},
    )
    current = await repo.db.coredump_files.find_one({"id": original["id"]})
    replacement = await freeze(repo, current)
    replacement_path = Path(replacement["snapshot"]["path"])

    reserved_token = uuid4().hex
    await _reserve(repo, reserved_token, original["id"], len(payload))
    await expire(repo, reserved_token)
    root = snapshots_root(repo.settings)
    temporary = root / f".{original['id']}-{original['version']}.{reserved_token}.partial"
    final = root / f"{original['id']}-{original['version']}.{reserved_token}.core"
    temporary.write_bytes(payload)
    final.write_bytes(payload)
    unrelated = root / "unregistered-file.core"
    unrelated.write_bytes(b"keep")

    examined = await verify_candidate_index(repo)
    await reconcile_snapshots(repo)
    assert not published_path.exists() and not temporary.exists() and not final.exists()
    for token in (published_token, reserved_token):
        claim = await repo.db.coredump_snapshot_claims.find_one({"id": token})
        assert claim["state"] == "RELEASED"
    assert replacement_path.read_bytes() == payload
    assert source.read_bytes() == payload and unrelated.read_bytes() == b"keep"
    remaining = await repo.db.coredump_snapshot_reservations.find_one({"id": repo.settings.node_id})
    assert remaining["used"] == len(payload)
    assert remaining["activeTokens"] == [replacement["snapshot"]["reservationToken"]]
    await reconcile_snapshots(repo)
    assert (await repo.db.coredump_snapshot_reservations.find_one({"id": repo.settings.node_id}))["used"] == len(payload)
    print(json.dumps({"passed": True, "publishedOrphanRemoved": True, "reservedOrphansRemoved": True,
                      "replacementRetained": True, "sourceRetained": True, "quotaExactlyOnce": True,
                      "candidateDocsExamined": examined, "releasedHistoryRows": 1000}))


async def main() -> None:
    """随机真实数据库与临时目录均在finally回收，不访问已有资源或设备。"""
    configured = Settings()
    database_name = "coredump_orphans_verify_" + uuid4().hex
    client = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                              w="majority", journal=True)
    try:
        with TemporaryDirectory(prefix="camera-coredump-orphans-") as directory:
            root = Path(directory)
            settings = Settings(_env_file=None, log_root=root / "logs", nfs_root=root / "nfs",
                                node_id="verify-node", encryption_key=Fernet.generate_key().decode())
            repo = Repository(client[database_name], settings)
            await repo.initialize()
            await verify(repo)
    finally:
        await client.drop_database(database_name)
        assert database_name not in await client.list_database_names()
        await client.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryDirectoryRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
