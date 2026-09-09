"""真实副本集验证节点删除回滚、调度竞争及心跳不复活，全部使用隔离数据。"""

import asyncio
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from camera_logs.common.database import now
from camera_logs.main import create_app
from camera_logs.node.worker import Worker
from camera_logs.tasks.claim import claim_task
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError


async def main():
    """不启动运行循环，不连接设备；模拟归属只存在于随机数据库。"""
    configured = Settings()
    name, token = "node_delete_" + uuid4().hex, uuid4().hex
    temporary = TemporaryDirectory(prefix="node-delete-")
    path = Path(temporary.name)
    mongo = AsyncMongoClient(configured.mongo_uri, tz_aware=True)
    settings = Settings(mongo_uri=configured.mongo_uri, database_name=name, bootstrap_token=token,
                        encryption_key=Fernet.generate_key().decode(), log_root=path / "logs", start_background=False,
                        node_id="heartbeat", node_url="http://worker:8001")
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            repo, db = app.state.repo, app.state.repo.db
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                         base_url="http://verify", headers={"Authorization": "Bearer " + token}) as client:
                async def seed(identifier):
                    await db.nodes.insert_one({"id": identifier, "url": "http://worker:8001", "capacity": 100,
                                               "heartbeat": now(), "diskPercent": 1, "activeTasks": 0, "accepting": True})

                async def remove(identifier):
                    return await client.delete(f"/api/v1/admin/nodes/{identifier}?version=0")

                original = repo.audit
                for identifier, failure in (("failure", PyMongoError("injected")), ("cancel", asyncio.CancelledError())):
                    await seed(identifier)
                    async def fail(*args, error=failure, **kwargs):
                        raise error
                    repo.audit = fail
                    try:
                        assert (await remove(identifier)).status_code >= 500
                    finally:
                        repo.audit = original
                    assert (await db.nodes.find_one({"id": identifier})).get("deletedAt") is None
                    assert await db.node_configs.count_documents({"id": identifier}) == 0
                    assert await db.audit.count_documents({"action": "delete_node", "targetId": identifier}) == 0

                for index in range(6):
                    identifier = f"race-{index}"
                    await seed(identifier)
                    task = {"id": identifier, "ip": "192.0.2.1", "port": 22, "nodeId": None,
                            "desiredState": "RUNNING", "status": "PENDING", "generation": 0}
                    await db.tasks.insert_one(task)
                    removed, claimed = await asyncio.gather(remove(identifier), claim_task(repo, task, identifier))
                    if removed.status_code == 204:
                        assert claimed is None
                        assert (await db.tasks.find_one({"id": identifier}))["nodeId"] is None
                    else:
                        assert removed.status_code == 409 and claimed is not None
                        assert (await db.nodes.find_one({"id": identifier})).get("deletedAt") is None

                await seed("heartbeat")
                archive = path / "retained.log"
                archive.write_bytes(b"retained historical log\n")
                await db.files.insert_one({"id": "retained", "nodeId": "heartbeat", "path": str(archive)})
                assert (await remove("heartbeat")).status_code == 204
                assert (await remove("heartbeat")).status_code == 204
                # 模拟旧心跳快照迟到覆盖 accepting，持久删除标记仍须阻止新领取。
                await db.nodes.update_one({"id": "heartbeat"}, {"$set": {"accepting": True}})
                late = {"id": "late", "ip": "192.0.2.2", "port": 22, "nodeId": None,
                        "desiredState": "RUNNING", "status": "PENDING", "generation": 0}
                await db.tasks.insert_one(late)
                assert await claim_task(repo, late, "heartbeat") is None
                worker = Worker(repo)
                worker.last_maintenance = time.monotonic()
                await worker.tick()
                node = await db.nodes.find_one({"id": "heartbeat"})
                assert node["deletedAt"] and not node["accepting"]
                items = (await client.get("/api/v1/admin/nodes")).json()["items"]
                assert all(item["id"] != "heartbeat" for item in items)
                assert archive.read_bytes() == b"retained historical log\n"
                assert await db.files.count_documents({"id": "retained"}) == 1
                assert await db.audit.count_documents({"action": "delete_node", "targetId": "heartbeat"}) == 1
    finally:
        await mongo.drop_database(name)
        assert name not in await mongo.list_database_names()
        await mongo.close()
        temporary.cleanup()
        assert not path.exists()
    print(json.dumps({"passed": True, "rollback": True, "claimRace": True,
                      "heartbeatDoesNotRestore": True, "historyPreserved": True, "temporaryDataRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
