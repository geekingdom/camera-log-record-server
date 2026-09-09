"""真实副本集验证日志作业、文件保护和审计的原子提交与取消。"""

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

HOUR = "2026-09-09T10:00:00+00:00"


async def main():
    """随机独立库内构造目录，所有真实采集任务、归档和设备连接均不触碰。"""
    configured = Settings()
    name, token = "job_submit_" + uuid4().hex, uuid4().hex
    temporary = TemporaryDirectory(prefix="job-submit-")
    path = Path(temporary.name)
    mongo = AsyncMongoClient(configured.mongo_uri, tz_aware=True)
    app = create_app(Settings(mongo_uri=configured.mongo_uri, database_name=name, bootstrap_token=token,
                              encryption_key=Fernet.generate_key().decode(), log_root=path / "logs",
                              start_background=False))
    try:
        async with app.router.lifespan_context(app):
            repo, db = app.state.repo, app.state.repo.db
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                         base_url="http://verify", headers={"Authorization": "Bearer " + token}) as client:
                async def seed(identifier):
                    await db.tasks.insert_one({"id": identifier, "name": "模拟任务", "ip": "192.0.2.1"})
                    await db.files.insert_one({"id": identifier, "taskId": identifier, "nodeId": "node",
                                               "status": "READY", "hour": HOUR, "bytes": 10})

                async def submit(identifier, kind):
                    payload = {"taskId": identifier}
                    if kind == "DOWNLOAD":
                        payload["hourIds"] = [HOUR]
                    else:
                        payload.update(keyword="needle", start=HOUR, end="2026-09-09T11:00:00+00:00")
                    endpoint = "downloads" if kind == "DOWNLOAD" else "log-searches"
                    return await client.post("/api/v1/" + endpoint, json=payload,
                                             headers={"Idempotency-Key": identifier})

                original = repo.audit
                for kind in ("DOWNLOAD", "SEARCH"):
                    for suffix, failure in (("failure", PyMongoError("injected")), ("cancel", asyncio.CancelledError())):
                        identifier = kind + "-" + suffix
                        await seed(identifier)
                        async def fail(*args, error=failure, **kwargs):
                            raise error
                        repo.audit = fail
                        try:
                            assert (await submit(identifier, kind)).status_code >= 500
                        finally:
                            repo.audit = original
                        assert await db.jobs.count_documents({"taskId": identifier}) == 0
                        assert await db.idempotency.count_documents({"key": identifier}) == 0
                        assert "retainUntil" not in await db.files.find_one({"id": identifier})
                        assert await db.audit.count_documents({"action": kind}) == 0

                await seed("concurrent")
                responses = await asyncio.gather(submit("concurrent", "DOWNLOAD"), submit("concurrent", "DOWNLOAD"))
                assert all(response.status_code == 202 for response in responses)
                identifiers = {response.json()["id"] for response in responses}
                assert len(identifiers) == 1
                identifier = identifiers.pop()
                assert await db.audit.count_documents({"action": "DOWNLOAD", "targetId": identifier}) == 1
                endpoint = "/api/v1/downloads/" + identifier
                async def fail_cancel(*args, **kwargs):
                    raise PyMongoError("cancel audit failure")
                repo.audit = fail_cancel
                try:
                    assert (await client.delete(endpoint)).status_code == 503
                finally:
                    repo.audit = original
                assert (await db.jobs.find_one({"id": identifier}))["status"] == "QUEUED"
                assert await db.audit.count_documents({"action": "cancel_download", "targetId": identifier}) == 0
                responses = await asyncio.gather(client.delete(endpoint), client.delete(endpoint))
                assert all(response.status_code == 204 for response in responses)
                assert await db.audit.count_documents({"action": "cancel_download", "targetId": identifier}) == 1

                await seed("lost-ack")
                transaction = audited_mutations.mutation_transaction
                async def lost_ack(repo, callback):
                    await transaction(repo, callback)
                    raise PyMongoError("lost acknowledgement")
                audited_mutations.mutation_transaction = lost_ack
                try:
                    response = await submit("lost-ack", "SEARCH")
                    assert response.status_code == 202
                    identifier = response.json()["id"]
                    assert (await client.delete("/api/v1/log-searches/" + identifier)).status_code == 204
                finally:
                    audited_mutations.mutation_transaction = transaction
                replay = await submit("lost-ack", "SEARCH")
                assert replay.status_code == 202 and replay.json()["id"] == identifier
                assert replay.json()["status"] == "CANCELLED"
                assert await db.jobs.count_documents({"taskId": "lost-ack"}) == 1
                assert await db.audit.count_documents({"targetId": identifier}) == 2
    finally:
        await mongo.drop_database(name)
        assert name not in await mongo.list_database_names()
        await mongo.close()
        temporary.cleanup()
        assert not path.exists()
    print(json.dumps({"passed": True, "protectionAndAuditRollback": True, "concurrentReplay": True,
                      "cancelAtomic": True, "unknownCommitReadOnly": True, "temporaryDataRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
