"""仓储幂等故障回归：重试复用资源身份，处理写入结果不确定及并发预留。"""
import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient


async def test_failed_create_retry_preserves_resource_identity(tmp_path):
    repo = Repository(AsyncMongoMockClient().db, Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))
    await repo.initialize()
    identifiers = []
    async def build(identifier):
        identifiers.append(identifier)
        if len(identifiers) == 1:
            raise OSError("temporary storage unavailable")
        doc = {"id": identifier}
        await repo.db.tasks.insert_one(doc)
        return doc
    with pytest.raises(HTTPException) as error:
        await repo.idem("user", "key", "create", {}, "tasks", build)
    assert error.value.status_code == 503
    result = await repo.idem("user", "key", "create", {}, "tasks", build)
    assert identifiers == [result["id"], result["id"]]
    assert await repo.db.tasks.count_documents({}) == 1
