"""节点配置必须影响实际准入，并且不能中断已有采集或伪造服务地址。"""
import time
from types import SimpleNamespace
from unittest.mock import patch

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.worker import Worker
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def test_configured_admission_and_capacity_apply_before_connections(tmp_path):
    repo = Repository(AsyncMongoMockClient().db, Settings(
        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_capacity=2))
    await repo.initialize()
    node_id = repo.settings.node_id
    await repo.db.node_configs.insert_one({"id": node_id, "capacity": 1, "accepting": False, "url": repo.settings.node_url})
    for identifier in ("one", "two"):
        await repo.db.tasks.insert_one({"id": identifier, "runId": identifier, "nodeId": node_id,
                                        "status": "PENDING", "desiredState": "RUNNING"})
    worker = Worker(repo)
    worker.last_maintenance = time.monotonic()
    with patch("camera_logs.node.worker.shutil.disk_usage", return_value=SimpleNamespace(used=50, total=100, free=50)), \
            patch("camera_logs.node.worker.SessionRuntime") as factory:
        await worker.tick()
        factory.assert_not_called()
        assert not (await repo.get("nodes", node_id))["accepting"]
        await repo.db.node_configs.update_one({"id": node_id}, {"$set": {"accepting": True}})
        await worker.tick()
        factory.assert_called_once()
        assert len(worker.active) == 1
        assert (await repo.get("nodes", node_id))["capacity"] == 1


async def test_mismatched_registered_url_does_not_replace_reported_address(tmp_path):
    repo = Repository(AsyncMongoMockClient().db, Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))
    await repo.initialize()
    await repo.db.node_configs.insert_one({"id": repo.settings.node_id, "capacity": 100, "accepting": True, "url": "https://other.example"})
    worker = Worker(repo)
    worker.last_maintenance = time.monotonic()
    await worker.tick()
    node = await repo.get("nodes", repo.settings.node_id)
    assert node["url"] == repo.settings.node_url
    assert node["configurationMismatch"] is True
    assert node["accepting"] is False
