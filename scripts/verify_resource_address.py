"""使用独立Mongo副本集库验证同IP并发创建、回滚和软删除释放，不连接设备。"""

import asyncio
import json
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common.audited_mutations import audited_create, audited_mutation
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.resources.address_claim import claim_address, release_address
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pymongo import AsyncMongoClient, ReturnDocument


async def main():
    """只使用随机库和临时目录；无论验证成功或失败均删除自建数据。"""
    configured = Settings()
    name = "address_verify_" + uuid4().hex
    mongo = AsyncMongoClient(configured.mongo_uri, tz_aware=True)
    with TemporaryDirectory(prefix="camera-address-") as temporary:
        settings = Settings(mongo_uri=configured.mongo_uri, database_name=name, log_root=temporary,
                            encryption_key=Fernet.generate_key().decode(), start_background=False)
        repo = Repository(mongo[name], settings)
        try:
            await repo.initialize()

            async def create(key, ip="192.0.2.99"):
                async def prepare(identifier):
                    return {"id": identifier, "ip": ip, "kind": "SERIAL_SERVER", "name": key,
                            "createdBy": "alice", "version": 1, "createdAt": now()}

                async def reserve(document, session):
                    await claim_address(repo, document, session)

                return await audited_create(repo, "alice", key, "create_resource", {"ip": ip},
                                            "resources", prepare, before_insert=reserve)

            results = await asyncio.gather(*(create(f"concurrent-{i}") for i in range(12)), return_exceptions=True)
            winners = [item for item in results if isinstance(item, dict)]
            assert len(winners) == 1, results
            assert all(isinstance(item, dict) or isinstance(item, HTTPException) and item.status_code == 409 for item in results)
            assert await repo.db.resources.count_documents({}) == 1
            assert await repo.db.resource_addresses.count_documents({}) == 1
            assert await repo.db.idempotency.count_documents({}) == 1
            original = repo.audit

            async def fail(*_args, **_kwargs):
                raise RuntimeError("synthetic audit failure")

            repo.audit = fail
            try:
                await create("rollback", "192.0.2.100")
                raise AssertionError("故障注入必须中断事务")
            except RuntimeError:
                pass
            finally:
                repo.audit = original
            assert await repo.db.resource_addresses.find_one({"_id": "192.0.2.100"}) is None
            assert await repo.db.resources.find_one({"ip": "192.0.2.100"}) is None
            assert await repo.db.idempotency.find_one({"key": "rollback"}) is None
            winner = winners[0]

            async def remove(session):
                changed = await repo.db.resources.find_one_and_update(
                    {"id": winner["id"]}, {"$set": {"deletedAt": now()}},
                    return_document=ReturnDocument.AFTER, session=session,
                )
                await release_address(repo, changed, session)
                return changed

            await audited_mutation(repo, "alice", "delete_resource", winner["id"], remove)
            replacement = await create("replacement")
            assert replacement["id"] != winner["id"]
            assert await repo.db.resources.count_documents({}) == 2
            assert await repo.db.resources.count_documents({"deletedAt": None}) == 1
            print(json.dumps({"passed": True, "concurrentCreates": 12, "winners": 1,
                              "auditFailureRollback": True, "softDeletionReleasesAddress": True}))
        finally:
            await mongo.drop_database(name)
            await mongo.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryDirectoryRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
