"""真实副本集验证下载票据与审计原子提交，结束后清理独立库和目录。"""

import asyncio
import hashlib
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
    """通过正式路由注入审计失败、取消和确认丢失，不访问真实任务。"""
    configured = Settings()
    name, token = "download_ticket_" + uuid4().hex, uuid4().hex
    temporary = TemporaryDirectory(prefix="download-ticket-")
    path = Path(temporary.name)
    mongo = AsyncMongoClient(configured.mongo_uri, tz_aware=True)
    app = create_app(Settings(mongo_uri=configured.mongo_uri, database_name=name,
                             bootstrap_token=token, encryption_key=Fernet.generate_key().decode(),
                             log_root=path / "logs", start_background=False))
    try:
        async with app.router.lifespan_context(app):
            repo, db = app.state.repo, app.state.repo.db
            await db.jobs.insert_one({"id": "export", "taskId": "task", "status": "SUCCEEDED"})
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                         base_url="http://verify", headers={"Authorization": "Bearer " + token}) as client:
                endpoint = "/api/v1/downloads/export/browser-session"
                original = repo.audit
                for failure in (PyMongoError("injected"), asyncio.CancelledError()):
                    async def fail(*args, error=failure, **kwargs):
                        raise error
                    repo.audit = fail
                    try:
                        response = await client.post(endpoint)
                        assert response.status_code >= 500
                        assert "set-cookie" not in response.headers
                    finally:
                        repo.audit = original
                    assert await db.download_sessions.count_documents({}) == 0
                    assert await db.audit.count_documents({"action": "browser_download_authorization"}) == 0

                transaction = audited_mutations.mutation_transaction
                async def lost_ack(repo, callback):
                    await transaction(repo, callback)
                    raise PyMongoError("lost acknowledgement")
                audited_mutations.mutation_transaction = lost_ack
                try:
                    response = await client.post(endpoint)
                    assert response.status_code == 200
                    credential = response.cookies.get("download_access")
                    stored = await db.download_sessions.find_one({
                        "tokenHash": hashlib.sha256(credential.encode()).hexdigest()})
                    assert stored and stored["jobId"] == "export"
                    assert "HttpOnly" in response.headers["set-cookie"]
                    assert await db.download_sessions.count_documents({}) == 1
                    assert await db.audit.count_documents({"action": "browser_download_authorization"}) == 1
                finally:
                    audited_mutations.mutation_transaction = transaction
    finally:
        await mongo.drop_database(name)
        assert name not in await mongo.list_database_names()
        await mongo.close()
        temporary.cleanup()
    assert not path.exists()
    print(json.dumps({"auditRollback": True, "cancelRollback": True, "lostAcknowledgementRecovered": True,
                      "temporaryDatabaseRemoved": True, "temporaryDataRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
