"""验证 IP 白名单恢复脚本只使用配置目标，并避免真实 MongoDB 的冲突更新路径。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from camera_logs.access_policy.policy import POLICY_ID
from camera_logs.common.config import Settings
from mongomock_motor import AsyncMongoMockClient

_SCRIPT = Path(__file__).parents[1] / "scripts" / "reset_ip_policy.py"
_SPEC = importlib.util.spec_from_file_location("reset_ip_policy", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_local_mongo = _MODULE._local_mongo
database_allowed = _MODULE.database_allowed
reset_ip_policy = _MODULE.reset_ip_policy


def test_database_allowance_defaults_to_loopback_and_requires_explicit_configured_remote_uri(tmp_path):
    """本机 URI 默认可用；副本集服务名仅在 Settings 显式提供且开关开启后可恢复。"""
    local = Settings(_env_file=None, mongo_uri="mongodb://127.0.0.1:27019/?directConnection=true", log_root=tmp_path)
    configured_remote = Settings(_env_file=None, mongo_uri="mongodb://mongo1:27017,mongo2:27017,mongo3:27017/camera_logs?replicaSet=rs0", log_root=tmp_path)
    unconfigured_remote = SimpleNamespace(mongo_uri="mongodb://mongo1:27017/camera_logs", model_fields_set=set())
    assert _local_mongo(local.mongo_uri)
    assert database_allowed(local, False)
    assert not _local_mongo(configured_remote.mongo_uri)
    assert not database_allowed(configured_remote, False)
    assert database_allowed(configured_remote, True)
    assert not database_allowed(unconfigured_remote, True)
    assert not _local_mongo("mongodb+srv://cluster.example/camera_logs")


@pytest.mark.asyncio
async def test_reset_policy_uses_nonconflicting_version_increment_and_audits_without_secrets():
    """新建与既有策略均只通过 $inc 推进版本，审计沿用 targetId 契约且不保存 URI。"""
    database = AsyncMongoMockClient().camera_logs
    created = await reset_ip_policy(database)
    assert created["id"] == POLICY_ID and created["enabled"] is False and created["version"] == 1
    await database.ip_policy.update_one({"id": POLICY_ID}, {"$set": {"enabled": True}})
    changed = await reset_ip_policy(database)
    assert changed["enabled"] is False and changed["version"] == 2
    audit = await database.audit.find_one({"action": "reset_ip_policy"})
    assert audit == {key: audit[key] for key in ("_id", "actor", "action", "targetId", "createdAt")}
    assert audit["actor"] == "local-ip-policy-recovery" and audit["targetId"] == POLICY_ID
