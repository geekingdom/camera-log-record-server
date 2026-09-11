"""增长型管理事件索引的初始化回归。"""

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.common.event_indexes import _create_or_reuse_index
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

EXPECTED_EVENT_INDEXES = {
    "audit": {
        "audit_created_at_id": (("createdAt", -1), ("_id", -1)),
        "audit_action_created_at_id": (("action", 1), ("createdAt", -1), ("_id", -1)),
        "audit_actor_created_at_id": (("actor", 1), ("createdAt", -1), ("_id", -1)),
        "audit_target_created_at_id": (("targetId", 1), ("createdAt", -1), ("_id", -1)),
        "audit_request_created_at_id": (("requestId", 1), ("createdAt", -1), ("_id", -1)),
    },
    "events": {
        "events_created_at_id": (("createdAt", -1), ("_id", -1)),
        "events_task_created_at_id": (("taskId", 1), ("createdAt", -1), ("_id", -1)),
        "events_node_created_at_id": (("nodeId", 1), ("createdAt", -1), ("_id", -1)),
        "events_type_created_at_id": (("type", 1), ("createdAt", -1), ("_id", -1)),
        "events_request_created_at_id": (("requestId", 1), ("createdAt", -1), ("_id", -1)),
    },
    "request_events": {
        "request_events_created_at_id": (("createdAt", -1), ("_id", -1)),
        "request_events_task_created_at_id": (("taskId", 1), ("createdAt", -1), ("_id", -1)),
        "request_events_status_created_at_id": (("httpStatus", 1), ("createdAt", -1), ("_id", -1)),
        "request_events_route_created_at_id": (("route", 1), ("createdAt", -1), ("_id", -1)),
        "request_events_client_ip_created_at_id": (("clientIp", 1), ("createdAt", -1), ("_id", -1)),
        "request_events_request_created_at_id": (("requestId", 1), ("createdAt", -1), ("_id", -1)),
    },
}


async def test_initialize_creates_indexes_for_actual_management_event_filters(tmp_path):
    """实际审计、运行和请求筛选字段都拥有时间倒序复合索引。"""
    database = AsyncMongoMockClient().db
    repository = Repository(database, Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))

    await repository.initialize()

    for collection, expected in EXPECTED_EVENT_INDEXES.items():
        indexes = await database[collection].index_information()
        assert {name: indexes[name]["key"] for name in expected} == expected
    ttl = (await database.request_events.index_information())["request_events_ttl_created_at"]
    assert tuple(ttl["key"]) == (("createdAt", 1),)
    assert ttl["expireAfterSeconds"] == 30 * 24 * 60 * 60


async def test_initialize_reuses_legacy_anonymous_event_indexes_and_is_idempotent(tmp_path):
    """既有匿名索引只按键复用，连续初始化不因改名或 TTL 重复失败。"""
    database = AsyncMongoMockClient().db
    await database.request_events.create_index("createdAt", expireAfterSeconds=30 * 24 * 60 * 60)
    await database.request_events.create_index([("taskId", 1), ("createdAt", -1)])
    await database.audit.create_index([("actor", 1), ("createdAt", -1)])
    repository = Repository(database, Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))

    await repository.initialize()
    await repository.initialize()

    request_indexes = await database.request_events.index_information()
    assert "createdAt_1" in request_indexes
    assert "taskId_1_createdAt_-1" in request_indexes
    assert request_indexes["createdAt_1"]["expireAfterSeconds"] == 30 * 24 * 60 * 60
    assert len([name for name, item in request_indexes.items()
                if item["key"] == [("createdAt", 1)]]) == 1


@pytest.mark.parametrize("options", [{"sparse": True}, {"partialFilterExpression": {"actor": {"$exists": True}}}])
async def test_reuse_rejects_incomplete_legacy_index_options(options):
    """partial 或 sparse 同键索引不能假定覆盖无筛选管理查询。"""
    collection = AsyncMongoMockClient().db.audit
    keys = (("actor", 1), ("createdAt", -1), ("_id", -1))
    await collection.create_index(keys, name="legacy_incomplete", **options)

    with pytest.raises(RuntimeError, match="需先迁移"):
        await _create_or_reuse_index(collection, keys, name="audit_actor_created_at_id")
