"""用真实副本集证明手动命令入队、幂等成功映射和审计共同回滚或提交。"""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from camera_logs.common import audited_mutations
from camera_logs.common.config import Settings
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError


async def main():
    """仅随机库内模拟采集状态，不启动Worker或打开任何设备连接。"""
    configured = Settings()
    name, token = "manual_submit_" + uuid4().hex, uuid4().hex
    temporary = TemporaryDirectory(prefix="manual-submit-")
    path = Path(temporary.name)
    mongo = AsyncMongoClient(configured.mongo_uri, tz_aware=True)
    settings = Settings(mongo_uri=configured.mongo_uri, database_name=name, bootstrap_token=token,
                        encryption_key=Fernet.generate_key().decode(), log_root=path / "logs", start_background=False)
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            repo, db = app.state.repo, app.state.repo.db
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                         base_url="http://verify", headers={"Authorization": "Bearer " + token}) as client:
                async def seed(task):
                    await db.tasks.insert_one({"id": task, "status": "COLLECTING", "desiredState": "RUNNING",
                                               "runId": "run", "sessionId": "session", "commandClaimVersion": 0})

                async def send(task):
                    return await client.post(f"/api/v1/tasks/{task}/commands", json={"command": "ls"},
                                             headers={"Idempotency-Key": task})

                original = repo.audit
                for task, error in (("audit-failure", PyMongoError("injected")), ("cancel", asyncio.CancelledError())):
                    await seed(task)
                    async def fail(*args, failure=error, **kwargs):
                        raise failure
                    repo.audit = fail
                    try:
                        response = await send(task)
                        assert response.status_code >= 500
                    finally:
                        repo.audit = original
                    assert await db.commands.count_documents({"taskId": task}) == 0
                    assert await db.idempotency.count_documents({"key": task}) == 0
                    assert (await db.tasks.find_one({"id": task}))["commandClaimVersion"] == 0
                    assert await db.audit.count_documents({"action": "command:" + task}) == 0

                await seed("concurrent")
                responses = await asyncio.gather(send("concurrent"), send("concurrent"))
                assert all(response.status_code == 202 for response in responses)
                assert len({response.json()["id"] for response in responses}) == 1
                assert await db.audit.count_documents({"action": "command:concurrent"}) == 1

                await seed("lost-ack")
                transaction = audited_mutations.mutation_transaction
                async def lose_ack(repo, callback):
                    await transaction(repo, callback)
                    raise PyMongoError("lost acknowledgement")
                audited_mutations.mutation_transaction = lose_ack
                try:
                    response = await send("lost-ack")
                    assert response.status_code == 202
                finally:
                    audited_mutations.mutation_transaction = transaction
                await db.tasks.update_one({"id": "lost-ack"}, {"$set": {"status": "STOPPED", "desiredState": "STOPPED"}})
                replay = await send("lost-ack")
                assert replay.status_code == 202 and replay.json()["id"] == response.json()["id"]
                assert await db.commands.count_documents({"taskId": "lost-ack"}) == 1
                assert await db.audit.count_documents({"action": "command:lost-ack"}) == 1
    finally:
        await mongo.drop_database(name)
        assert name not in await mongo.list_database_names()
        await mongo.close()
        temporary.cleanup()
        assert not path.exists()
    print(json.dumps({"passed": True, "auditAndQueueAtomic": True, "cancelRollback": True,
                      "concurrentReplay": True, "unknownCommitReadOnly": True, "temporaryDataRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
