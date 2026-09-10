"""在独立 Mongo 副本集库验证资源恢复消费与用户停止竞争，不访问任何设备。"""

import asyncio
import json
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.resources.health import reconcile_authorized_recoveries
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
            marker = {"resourceId": "resource", "desiredState": "RUNNING", "authorizedAt": now()}
            await repo.db.tasks.insert_one({"id": "task", "resourceId": "resource", "desiredState": "STOPPED",
                                            "status": "STOPPED", "nodeId": None, "protocol": "SSH", "ip": "192.0.2.250",
                                            "port": 22, "createdBy": "owner", "resourceHealthRecovery": marker})
            user = {"id": "owner", "isAdmin": True, "scopes": ["*"]}
            await asyncio.gather(reconcile_authorized_recoveries(repo), request_control(repo, "task", "STOPPED", user))
            task = await repo.db.tasks.find_one({"id": "task"})
            assert task["desiredState"] == "STOPPED" and "resourceHealthRecovery" not in task
            assert await repo.db.operations.count_documents({"taskId": "task", "desiredState": "RUNNING", "status": "PENDING"}) == 0
            print(json.dumps({"passed": True, "consumeStopRace": True, "noDeviceAccess": True}))
        finally:
            await mongo.drop_database(name)
            assert name not in await mongo.list_database_names()
            await mongo.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryDirectoryRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
