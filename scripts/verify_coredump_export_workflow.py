"""隔离验证 NFS coredump 扫描、冻结、单件/ZIP 导出、取消与 TTL 清理。"""

import argparse
import asyncio
import hashlib
import json
import zipfile
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.coredumps.jobs import _reserve, cleanup_expired_exports, run_export
from camera_logs.coredumps.scanner import CoredumpScanner
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


def digest(path: Path) -> str:
    """以流式摘要验证导出正文，验证脚本不将 coredump 整体读入内存。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source(file: dict) -> dict:
    """复制作业提交时固定的版本描述，禁止后续 scanner 变化替换导出源。"""
    return {key: file[key] for key in ("id", "nodeId", "resourceId", "version", "source")}


async def insert_job(repo: Repository, identifier: str, files: list[dict]) -> dict:
    """插入已由单一验证 Worker 领取的导出作业，供 run_export 走正式收尾逻辑。"""
    job = {"id": identifier, "actor": "verify", "status": "RUNNING", "workerInstanceId": "verify-worker",
           "coordinatorNodeId": repo.settings.node_id, "estimatedBytes": sum(item["size"] for item in files),
           "sources": [source(item) for item in files], "expiresAt": now() + timedelta(hours=1),
           "leaseUntil": now() + timedelta(seconds=90)}
    await repo.db.coredump_exports.insert_one(job)
    return job


async def verify() -> None:
    """用两台伪设备的本地 NFS 目录完整验证，不访问设备网络或开发数据。"""
    configured = Settings()
    database_name = "coredump_export_workflow_" + uuid4().hex
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    with TemporaryDirectory(prefix="camera-coredump-workflow-") as temporary:
        root = Path(temporary)
        settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=database_name,
                            log_root=root / "logs", nfs_root=root / "nfs", node_id="verify-node",
                            encryption_key=Fernet.generate_key().decode(), internal_token=uuid4().hex,
                            coredump_export_quota_bytes=20_000_000, start_background=False)
        settings.nfs_root.mkdir(parents=True)
        repo = Repository(mongo[database_name], settings)
        try:
            await repo.initialize()
            payloads = {"192.0.2.80": b"first coredump\n", "192.0.2.81": b"second coredump\n"}
            for index, (ip, body) in enumerate(payloads.items(), 1):
                directory = settings.nfs_root / ip
                directory.mkdir()
                (directory / f"core-{index}.bin").write_bytes(body)
                await repo.db.resources.insert_one({"id": f"camera-{index}", "ip": ip,
                                                    "kind": "HIKVISION_NETWORK", "deletedAt": None})
            assert await CoredumpScanner(repo).scan_once() == 2
            files = [item async for item in repo.db.coredump_files.find({}).sort("resourceId", 1)]

            single = await insert_job(repo, uuid4().hex, [files[0]])
            assert (await run_export(repo, single))["status"] == "SUCCEEDED"
            single_result = await repo.db.coredump_exports.find_one({"id": single["id"]})
            single_path = Path(single_result["resultPath"])
            assert digest(single_path) == hashlib.sha256(payloads["192.0.2.80"]).hexdigest()

            multiple = await insert_job(repo, uuid4().hex, files)
            assert (await run_export(repo, multiple))["status"] == "SUCCEEDED"
            multiple_result = await repo.db.coredump_exports.find_one({"id": multiple["id"]})
            with zipfile.ZipFile(multiple_result["resultPath"]) as archive:
                members = archive.namelist()
                assert len(members) == 2 and len(set(members)) == 2
                assert sorted(archive.read(name) for name in members) == sorted(payloads.values())

            cancelled = await insert_job(repo, uuid4().hex, files)
            await _reserve(repo, cancelled)
            await repo.db.coredump_exports.update_one({"id": cancelled["id"]}, {"$set": {"status": "CANCELLED"}})
            assert (await run_export(repo, cancelled))["status"] == "CANCELLED"
            claim = await repo.db.coredump_export_reservation_claims.find_one({"id": cancelled["id"]})
            assert claim["state"] == "RELEASED"

            await repo.db.coredump_exports.delete_one({"id": multiple["id"]})
            assert await cleanup_expired_exports(repo) >= 1
            assert not Path(multiple_result["resultPath"]).parent.exists()
            claim = await repo.db.coredump_export_reservation_claims.find_one({"id": multiple["id"]})
            assert claim["state"] == "RELEASED"
            print(json.dumps({"passed": True, "singleDigest": single_result["etag"], "zipMembers": len(members),
                              "cancelledReleased": True, "ttlReleased": True,
                              "scope": "local temporary NFS paths and random Mongo database"}))
        finally:
            await mongo.drop_database(database_name)
            await mongo.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryDirectoryRemoved": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="验证 coredump 导出闭环")
    parser.parse_args()
    asyncio.run(verify())
