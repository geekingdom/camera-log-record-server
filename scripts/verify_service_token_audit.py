"""真实副本集验证令牌创建、撤销及审计原子性，不启动采集或连接实体设备。"""

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
    """用正式接口注入异常和确认丢失；所有凭据及日志仅存在于临时验证环境。"""
    configured = Settings()
    name, token = "token_audit_" + uuid4().hex, uuid4().hex
    temporary = TemporaryDirectory(prefix="token-audit-")
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
                async def create(label):
                    return await client.post("/api/v1/service-tokens", json={
                        "name": label, "userId": "builtin-admin", "expiresInDays": 1,
                    })

                original = repo.audit
                for label, failure in (("db-failure", PyMongoError("injected")),
                                       ("cancel", asyncio.CancelledError())):
                    async def fail(*args, error=failure, **kwargs):
                        raise error
                    repo.audit = fail
                    try:
                        assert (await create(label)).status_code >= 500
                    finally:
                        repo.audit = original
                    assert await db.tokens.count_documents({"name": label}) == 0
                    assert await db.audit.count_documents({"action": "create_token"}) == 0

                created = await create("revoke")
                assert created.status_code == 201
                identifier = created.json()["id"]
                url = "/api/v1/service-tokens/" + identifier
                async def fail_revoke(*args, **kwargs):
                    raise PyMongoError("injected")
                repo.audit = fail_revoke
                try:
                    assert (await client.delete(url)).status_code == 503
                finally:
                    repo.audit = original
                assert not (await db.tokens.find_one({"id": identifier}))["revoked"]
                assert await db.audit.count_documents({"action": "revoke_token", "targetId": identifier}) == 0
                responses = await asyncio.gather(client.delete(url), client.delete(url))
                assert all(response.status_code == 204 for response in responses)
                assert await db.audit.count_documents({"action": "revoke_token", "targetId": identifier}) == 1
                assert (await client.get("/api/v1/tasks", headers={
                    "Authorization": "Bearer " + created.json()["token"],
                })).status_code == 401

                transaction = audited_mutations.mutation_transaction
                async def lost_ack(repo, callback):
                    await transaction(repo, callback)
                    raise PyMongoError("lost acknowledgement")
                audited_mutations.mutation_transaction = lost_ack
                try:
                    recovered = await create("lost-ack")
                    assert recovered.status_code == 201
                    payload = recovered.json()
                    stored = await db.tokens.find_one({"id": payload["id"]})
                    assert stored["tokenHash"] == hashlib.sha256(payload["token"].encode()).hexdigest()
                    assert "token" not in stored and "tokenHash" not in payload
                    assert await db.tokens.count_documents({"name": "lost-ack"}) == 1
                    assert (await client.delete("/api/v1/service-tokens/" + payload["id"])).status_code == 204
                finally:
                    audited_mutations.mutation_transaction = transaction
                for action in ("create_token", "revoke_token"):
                    assert await db.audit.count_documents({"action": action, "targetId": payload["id"]}) == 1

                updated = await create("update")
                assert updated.status_code == 201
                update_id = updated.json()["id"]
                async def fail_update(*args, **kwargs):
                    raise PyMongoError("injected")
                repo.audit = fail_update
                try:
                    rejected = await client.patch("/api/v1/service-tokens/" + update_id, json={
                        "version": 1, "name": "must-not-commit",
                    })
                    assert rejected.status_code == 503
                finally:
                    repo.audit = original
                assert (await db.tokens.find_one({"id": update_id}))["name"] == "update"
                assert await db.audit.count_documents({"action": "update_token", "targetId": update_id}) == 0

                transaction = audited_mutations.mutation_transaction
                async def lost_update_ack(repo, callback):
                    await transaction(repo, callback)
                    raise PyMongoError("lost acknowledgement")
                audited_mutations.mutation_transaction = lost_update_ack
                try:
                    recovered = await client.patch("/api/v1/service-tokens/" + update_id, json={
                        "version": 1, "name": "confirmed-after-lost-ack",
                    })
                    assert recovered.status_code == 200
                    assert recovered.json()["name"] == "confirmed-after-lost-ack"
                finally:
                    audited_mutations.mutation_transaction = transaction
                assert await db.audit.count_documents({"action": "update_token", "targetId": update_id}) == 1

                rotating = await create("rotate")
                assert rotating.status_code == 201
                rotating_payload = rotating.json()
                rotate_url = "/api/v1/service-tokens/" + rotating_payload["id"] + "/rotate"

                async def fail_rotate(*args, **kwargs):
                    raise PyMongoError("injected")
                repo.audit = fail_rotate
                try:
                    rejected = await client.post(rotate_url, json={"version": rotating_payload["version"]})
                    assert rejected.status_code == 503
                    assert rotating_payload["token"] not in rejected.text
                finally:
                    repo.audit = original
                unchanged = await db.tokens.find_one({"id": rotating_payload["id"]})
                assert unchanged["tokenHash"] == hashlib.sha256(rotating_payload["token"].encode()).hexdigest()
                assert await db.audit.count_documents({"action": "rotate_token", "targetId": rotating_payload["id"]}) == 0

                transaction = audited_mutations.mutation_transaction
                async def lost_rotate_ack(repo, callback):
                    await transaction(repo, callback)
                    raise PyMongoError("lost acknowledgement")
                audited_mutations.mutation_transaction = lost_rotate_ack
                try:
                    recovered = await client.post(rotate_url, json={"version": rotating_payload["version"]})
                    assert recovered.status_code == 200
                    rotated_payload = recovered.json()
                    assert rotated_payload["token"] != rotating_payload["token"]
                    assert (await client.get("/api/v1/tasks", headers={
                        "Authorization": "Bearer " + rotating_payload["token"],
                    })).status_code == 401
                    assert (await client.get("/api/v1/tasks", headers={
                        "Authorization": "Bearer " + rotated_payload["token"],
                    })).status_code == 200
                finally:
                    audited_mutations.mutation_transaction = transaction
                stored = await db.tokens.find_one({"id": rotating_payload["id"]})
                assert stored["tokenHash"] == hashlib.sha256(rotated_payload["token"].encode()).hexdigest()
                assert stored["tokenEncrypted"] != rotated_payload["token"]
                assert await db.audit.count_documents({"action": "rotate_token", "targetId": rotating_payload["id"]}) == 1

                async def fail_reveal(*args, **kwargs):
                    raise PyMongoError("injected")
                repo.audit = fail_reveal
                try:
                    rejected = await client.post("/api/v1/service-tokens/" + rotating_payload["id"] + "/reveal")
                    assert rejected.status_code == 503
                    assert rotated_payload["token"] not in rejected.text
                finally:
                    repo.audit = original
    finally:
        await mongo.drop_database(name)
        assert name not in await mongo.list_database_names()
        await mongo.close()
        temporary.cleanup()
        assert not path.exists()
    print(json.dumps({"passed": True, "createRollback": True, "revokeRollback": True,
                      "concurrentRevoke": True, "updateRollback": True, "updateUnknownCommitReadOnly": True,
                      "rotateRollback": True, "rotateUnknownCommitReadOnly": True, "revealFailureNoSecret": True,
                      "unknownCommitReadOnly": True, "temporaryDataRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())
