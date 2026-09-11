"""在真实Mongo随机库验证手动认证历史、事务头和审计的原子性，不连接设备。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from camera_logs.common.database import now
from camera_logs.main import create_app
from camera_logs.resources.authentication_records import record_authentication
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError


async def verify_case(api, repo, *, failure_status, fail_at):
    """网络结果只模拟一次，分别在审计或已写历史后注入故障并检查三集合回滚。"""
    identifier = uuid4().hex
    body = {"name": "synthetic-auth", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.35",
            "username": "synthetic", "password": "synthetic-password"}
    resource = {key: value for key, value in body.items() if key != "password"}
    await repo.db.resources.insert_one({"id": identifier, **resource, "passwordEncrypted": "", "createdBy": "bootstrap",
                                       "deletedAt": None, "authenticatedAt": now(), "version": 1})
    metadata = {"model": "synthetic-model", "subSerialNumber": "synthetic-serial", "softwareVersion": "test"}
    probe = AsyncMock(side_effect=HTTPException(failure_status, "模拟认证失败") if failure_status else None,
                      return_value=metadata)
    original_audit = repo.audit

    async def failed_history(*args, **kwargs):
        await record_authentication(*args, **kwargs)
        raise PyMongoError("synthetic history failure")

    history = failed_history if fail_at == "history" else record_authentication
    if fail_at == "audit":
        repo.audit = AsyncMock(side_effect=PyMongoError("synthetic audit failure"))
    try:
        with patch("camera_logs.resources.api._verified_metadata", probe), \
                patch("camera_logs.resources.api.record_authentication", history):
            response = await api.post(f"/api/v1/resources/{identifier}/authenticate", json=body)
        assert response.status_code == 503, response.status_code
        assert probe.await_count == 1, "数据库失败不得重复设备HTTP认证"
        assert await repo.db.authentication_records.count_documents({"resourceId": identifier}) == 0
        assert await repo.db.authentication_record_heads.find_one({"_id": identifier}) is None
        assert await repo.db.audit.count_documents({"targetId": identifier}) == 0
    finally:
        repo.audit = original_audit

    with patch("camera_logs.resources.api._verified_metadata", probe):
        response = await api.post(f"/api/v1/resources/{identifier}/authenticate", json=body)
    assert response.status_code == (failure_status or 200)
    assert await repo.db.authentication_records.count_documents({"resourceId": identifier}) == 1
    assert await repo.db.audit.count_documents({"targetId": identifier}) == 1
    assert (await repo.db.authentication_record_heads.find_one({"_id": identifier}))["revision"] == 1
    return {"deviceStatus": failure_status or 200, "failedWrite": fail_at, "rollback": True, "retryPaired": True}


async def main():
    """所有API走正式路由与真实事务；最终删除随机库和临时服务日志。"""
    configured = Settings()
    name = "manual_auth_audit_verify_" + uuid4().hex
    client = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    results = []
    with TemporaryDirectory(prefix="manual-auth-audit-") as directory:
        settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=name,
                            bootstrap_token=uuid4().hex, encryption_key=Fernet.generate_key().decode(),
                            admin_password="", start_background=False, log_root=Path(directory) / "logs")
        try:
            app = create_app(settings, client[name])
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://verify", headers={
                    "Authorization": "Bearer " + settings.bootstrap_token,
                }) as api:
                    for status in (None, 401, 503):
                        for fail_at in ("audit", "history"):
                            results.append(await verify_case(api, app.state.repo, failure_status=status, fail_at=fail_at))
        finally:
            await client.drop_database(name)
            removed = name not in await client.list_database_names()
            await client.close()
    assert removed and not Path(directory).exists()
    print(json.dumps({"passed": True, "cases": results, "temporaryDatabaseRemoved": removed,
                      "temporaryFilesRemoved": True}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
