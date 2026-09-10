"""在独立 Mongo 副本集库验证资源离线恢复、停止竞争和运行锁保护。"""

import asyncio
import json
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.resources.health import _apply_failure, _apply_success, reconcile_authorized_recoveries
from camera_logs.tasks.control import request_control
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


async def main():
    """只创建随机验证库和临时日志目录，finally 中无条件删除两者。"""
    configured, name = Settings(), "resource_health_verify_" + uuid4().hex
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    with TemporaryDirectory(prefix="resource-health-") as directory:
        settings = Settings(mongo_uri=configured.mongo_uri, database_name=name, log_root=directory,
                            encryption_key=Fernet.generate_key().decode(), bootstrap_token="verify", start_background=False)
        repo = Repository(mongo[name], settings)
        try:
            await repo.initialize()
            await repo.db.resources.insert_one({"id": "resource", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.250",
                                                "healthStatus": "ONLINE", "deletedAt": None})
            await repo.db.tasks.insert_one({"id": "offline-recover", "resourceId": "resource", "desiredState": "RUNNING",
                                            "status": "COLLECTING", "nodeId": "node-a", "runId": "offline-run",
                                            "protocol": "SSH", "ip": "192.0.2.250", "port": 22, "createdBy": "owner"})
            # 真实离线停止先保留旧运行归属；Worker 收尾后才能允许 ONLINE 消费恢复。
            await _apply_failure(repo, await repo.db.resources.find_one({"id": "resource"}), "OFFLINE")
            offline = await repo.db.tasks.find_one({"id": "offline-recover"})
            assert offline["desiredState"] == "STOPPED"
            assert offline["resourceHealthRecovery"]["reason"] == "OFFLINE"
            await repo.db.runs.insert_one({"id": "offline-run", "endedAt": now()})
            await repo.db.tasks.update_one({"id": "offline-recover"}, {"$set": {"status": "STOPPED", "nodeId": None}})
            await _apply_success(repo, await repo.db.resources.find_one({"id": "resource"}), {})
            await reconcile_authorized_recoveries(repo)
            recovered = await repo.db.tasks.find_one({"id": "offline-recover"})
            assert (recovered["desiredState"], recovered["status"]) == ("RUNNING", "STOPPED")
            assert "resourceHealthRecovery" not in recovered

            marker = {"resourceId": "resource", "desiredState": "RUNNING", "reason": "OFFLINE", "authorizedAt": now()}
            await repo.db.tasks.insert_one({"id": "stop-race", "resourceId": "resource", "desiredState": "STOPPED",
                                            "status": "STOPPED", "nodeId": None, "protocol": "SSH", "ip": "192.0.2.250",
                                            "port": 22, "createdBy": "owner", "resourceHealthRecovery": marker})
            user = {"id": "owner", "isAdmin": True, "scopes": ["*"]}
            await asyncio.gather(reconcile_authorized_recoveries(repo), request_control(repo, "stop-race", "STOPPED", user))
            task = await repo.db.tasks.find_one({"id": "stop-race"})
            assert task["desiredState"] == "STOPPED" and "resourceHealthRecovery" not in task
            assert await repo.db.operations.count_documents({"taskId": "stop-race", "desiredState": "RUNNING", "status": "PENDING"}) == 0

            await repo.db.tasks.insert_one({"id": "locked", "resourceId": "resource", "desiredState": "STOPPED",
                                            "status": "BLOCKED", "nodeId": None, "runId": "locked-run", "protocol": "SSH",
                                            "ip": "192.0.2.250", "port": 22, "createdBy": "owner",
                                            "resourceHealthRecovery": marker})
            await repo.db.runs.insert_one({"id": "locked-run", "endedAt": now()})
            await repo.db.endpoint_locks.insert_one({"taskId": "locked", "runId": "locked-run"})
            await reconcile_authorized_recoveries(repo)
            locked = await repo.db.tasks.find_one({"id": "locked"})
            assert (locked["desiredState"], locked["status"], locked["nodeId"]) == ("STOPPED", "BLOCKED", None)
            assert locked["resourceHealthRecovery"]["authorizedAt"]
            print(json.dumps({"passed": True, "offlineOnlineRecovery": True, "consumeStopRace": True,
                              "unfinishedRunLockProtected": True, "noDeviceAccess": True}))
        finally:
            await mongo.drop_database(name)
            assert name not in await mongo.list_database_names()
            await mongo.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryDirectoryRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
