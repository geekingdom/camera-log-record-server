"""仓储幂等故障回归：重试复用资源身份，处理写入结果不确定及并发预留。"""
import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient
from pymongo.errors import DuplicateKeyError


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


async def test_initialize_does_not_change_existing_task_state(tmp_path):
    """开发期初始化只创建索引，不应依据历史错误文本修改任务状态。"""
    database = AsyncMongoMockClient().db
    await database.tasks.insert_one(
        {"id": "blocked-task", "nodeId": None, "desiredState": "RUNNING", "status": "BLOCKED",
         "error": "采集端点已被活动任务占用"}
    )
    repo = Repository(database, Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))

    await repo.initialize()

    task = await repo.get("tasks", "blocked-task")
    assert task["status"] == "BLOCKED"
    assert task["error"] == "采集端点已被活动任务占用"


async def test_initialize_replaces_legacy_template_name_index_with_owner_name_index(tmp_path):
    """模板索引迁移不删除文档，允许不同创建者同名并拒绝同创建者重复名称。"""
    database = AsyncMongoMockClient().db
    await database.templates.create_index("name", unique=True)
    await database.templates.insert_one({"id": "legacy-template", "name": "同名", "createdBy": "owner-a"})
    repo = Repository(database, Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))

    await repo.initialize()

    indexes = await database.templates.index_information()
    assert "name_1" not in indexes
    assert indexes["createdBy_1_name_1"]["unique"] is True
    await database.templates.insert_one({"id": "other-owner-template", "name": "同名", "createdBy": "owner-b"})
    with pytest.raises(DuplicateKeyError):
        await database.templates.insert_one({"id": "duplicate-owner-template", "name": "同名", "createdBy": "owner-a"})
