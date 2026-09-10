"""资源级 Coredump 租约协调器回归。"""

from datetime import UTC, timedelta

from camera_logs.collection.coredump_lease import release_coredump_lease
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def test_release_coredump_lease_keeps_successor_lease_and_releases_matching_owner(tmp_path):
    """释放条件必须包含任务、运行、代次和节点，避免旧会话缩短后继租约。"""
    repo = Repository(AsyncMongoMockClient().camera_logs, Settings(
        _env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node-a"))
    task = {"id": "task-a", "runId": "run-a", "generation": 3, "nodeId": "node-a", "resourceId": "resource-a"}
    successor_until = now() + timedelta(seconds=70)
    await repo.db.resources.insert_one({"id": "resource-a", "coredumpLeaseTaskId": "task-b",
                                        "coredumpLeaseRunId": "run-b", "coredumpLeaseGeneration": 4,
                                        "coredumpLeaseNodeId": "node-b", "coredumpLeaseUntil": successor_until})

    await release_coredump_lease(repo, task)
    stored = (await repo.db.resources.find_one({"id": "resource-a"}))["coredumpLeaseUntil"].replace(tzinfo=UTC)
    assert stored > now() + timedelta(seconds=60)

    await repo.db.resources.update_one({"id": "resource-a"}, {"$set": {
        "coredumpLeaseTaskId": task["id"], "coredumpLeaseRunId": task["runId"],
        "coredumpLeaseGeneration": task["generation"], "coredumpLeaseNodeId": task["nodeId"],
        "coredumpLeaseUntil": successor_until}})
    await release_coredump_lease(repo, task)
    released = (await repo.db.resources.find_one({"id": "resource-a"}))["coredumpLeaseUntil"].replace(tzinfo=UTC)
    assert released <= now()
