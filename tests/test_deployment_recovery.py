"""验证受控 Docker 部署的旧 Worker 关闭证明和保守任务释放。"""

from camera_logs.common import audited_mutations
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.deployment_recovery import complete_deployment_recovery, prepare_deployment_recovery
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


def repository(tmp_path):
    """创建独立副本库，避免部署证明测试接触开发数据库。"""
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(),
                        log_root=tmp_path, node_id="worker-old")
    return Repository(AsyncMongoMockClient().db, settings)


async def transactionless(repo, callback):
    """MongoMock 不支持事务协议，单元测试仅验证 callback 内的精确 CAS 条件。"""
    return await callback(None)


async def insert_owned_run(repo, *, desired_state="RUNNING", session="session-old", run_session=None):
    """插入带精确旧 owner 的任务、运行和端点锁。"""
    task = {"id": "task", "runId": "run", "nodeId": "worker-old", "generation": 7,
            "sessionId": session, "desiredState": desired_state, "status": "COLLECTING",
            "controlClaimVersion": 4, "controlOperationId": "control", "ip": "192.0.2.1", "port": 22}
    await repo.db.tasks.insert_one(task)
    await repo.db.runs.insert_one({"id": "run", "taskId": "task", "nodeId": "worker-old", "generation": 7,
                                   "sessionId": run_session if run_session is not None else session})
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "run", "endpoint": "192.0.2.1:22"})
    await repo.db.commands.insert_many([
        {"id": "sending", "taskId": "task", "runId": "run", "status": "SENDING"},
        {"id": "queued", "taskId": "task", "runId": "run", "status": "QUEUED"},
    ])
    return task


async def test_complete_proves_stopped_old_run_and_releases_only_matching_running_owner(tmp_path, monkeypatch):
    """Docker 已确认停止后，完整 token 和 owner 才能释放锁供其它健康节点优先领取。"""
    repo = repository(tmp_path)
    await insert_owned_run(repo)
    monkeypatch.setattr(audited_mutations, "mutation_transaction", transactionless)

    prepared = await prepare_deployment_recovery(repo, "worker-old", token="a" * 32)
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {"status": "BLOCKED"}})
    completed = await complete_deployment_recovery(repo, "worker-old", "a" * 32)

    assert prepared["prepared"] == 1 and completed["proved"] == completed["released"] == 1
    task = await repo.db.tasks.find_one({"id": "task"})
    run = await repo.db.runs.find_one({"id": "run"})
    assert task["desiredState"] == "RUNNING" and task["nodeId"] is None and task["status"] == "STOPPED"
    assert await repo.db.endpoint_locks.find_one({"taskId": "task"}) is None
    assert (await repo.db.commands.find_one({"id": "sending"}))["status"] == "UNKNOWN"
    assert (await repo.db.commands.find_one({"id": "queued"}))["status"] == "CANCELLED"
    assert run["deploymentClosedProof"] == {
        "token": "a" * 32, "nodeId": "worker-old", "taskId": "task", "runId": "run",
        "generation": 7, "sessionId": "session-old", "confirmedAt": run["deploymentClosedProof"]["confirmedAt"],
    }


async def test_complete_fixes_legacy_run_session_only_to_frozen_task_owner(tmp_path, monkeypatch):
    """旧 run 缺会话字段时，只能在关闭证明 CAS 中补写 prepare 已冻结的精确身份。"""
    repo = repository(tmp_path)
    await insert_owned_run(repo)
    await repo.db.runs.update_one({"id": "run"}, {"$unset": {"sessionId": ""}})
    monkeypatch.setattr(audited_mutations, "mutation_transaction", transactionless)

    await prepare_deployment_recovery(repo, "worker-old", token="e" * 32)
    await complete_deployment_recovery(repo, "worker-old", "e" * 32)

    run = await repo.db.runs.find_one({"id": "run"})
    assert run["sessionId"] == "session-old"
    assert run["deploymentClosedProof"]["sessionId"] == "session-old"


async def test_complete_proves_frozen_files_after_old_worker_already_released_task(tmp_path, monkeypatch):
    """旧 Worker 优雅收尾先释放 task 时，Docker 停止后的历史文件仍需获得精确 proof。"""
    repo = repository(tmp_path)
    await insert_owned_run(repo)
    await repo.db.files.insert_one({"id": "old-file", "taskId": "task", "runId": "run", "nodeId": "worker-old",
        "sessionId": "earlier", "status": "OPEN", "path": str(tmp_path / "earlier.log"), "bytes": 4})
    monkeypatch.setattr(audited_mutations, "mutation_transaction", transactionless)
    await prepare_deployment_recovery(repo, "worker-old", token="f" * 32)
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {"nodeId": None}, "$unset": {"deploymentRecovery": ""}})

    completed = await complete_deployment_recovery(repo, "worker-old", "f" * 32)

    file = await repo.db.files.find_one({"id": "old-file"})
    assert completed["proved"] == completed["taskChanged"] == 1
    assert file["deploymentClosedProof"]["sessionId"] == "earlier"


async def test_complete_preserves_paused_task_but_still_proves_old_run_for_archive_recovery(tmp_path, monkeypatch):
    """用户在部署窗口暂停后不自动重新采集，已停止旧运行仍有归档恢复证明。"""
    repo = repository(tmp_path)
    await insert_owned_run(repo)
    monkeypatch.setattr(audited_mutations, "mutation_transaction", transactionless)

    await prepare_deployment_recovery(repo, "worker-old", token="b" * 32)
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {"desiredState": "PAUSED", "status": "PAUSED"}})
    completed = await complete_deployment_recovery(repo, "worker-old", "b" * 32)

    task = await repo.db.tasks.find_one({"id": "task"})
    run = await repo.db.runs.find_one({"id": "run"})
    assert completed["proved"] == completed["controlChanged"] == 1 and "released" not in completed
    assert task["desiredState"] == "PAUSED" and task["nodeId"] is None and task["status"] == "PAUSED"
    assert await repo.db.endpoint_locks.find_one({"taskId": "task", "runId": "run"}) is None
    assert run.get("endedAt") is None
    assert run["deploymentClosedProof"]["token"] == "b" * 32


async def test_prepare_rejects_incomplete_or_mismatched_run_identity(tmp_path, monkeypatch):
    """未持有完整会话身份或 run 不一致时不能留下可误用的部署恢复标记。"""
    repo = repository(tmp_path)
    await insert_owned_run(repo, session=None)
    monkeypatch.setattr(audited_mutations, "mutation_transaction", transactionless)

    incomplete = await prepare_deployment_recovery(repo, "worker-old", token="c" * 32)
    assert incomplete["incompleteOwner"] == 1
    assert "deploymentRecovery" not in await repo.db.tasks.find_one({"id": "task"})

    await repo.db.tasks.update_one({"id": "task"}, {"$set": {"sessionId": "session-old"}})
    await repo.db.runs.update_one({"id": "run"}, {"$set": {"sessionId": "other-session"}})
    mismatched = await prepare_deployment_recovery(repo, "worker-old", token="d" * 32)
    assert mismatched["runSessionConflict"] == 1
    assert "deploymentRecovery" not in await repo.db.runs.find_one({"id": "run"})


async def test_complete_old_proof_does_not_touch_successor_run_or_lock(tmp_path, monkeypatch):
    """部署停止期间已迁移的新运行不妨碍旧文件证明，且后继归属与锁不变。"""
    repo = repository(tmp_path)
    await insert_owned_run(repo)
    monkeypatch.setattr(audited_mutations, "mutation_transaction", transactionless)
    await prepare_deployment_recovery(repo, "worker-old", token="1" * 32)
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {
        "nodeId": "worker-new", "runId": "run-new", "sessionId": "session-new",
    }, "$unset": {"deploymentRecovery": ""}})
    await repo.db.endpoint_locks.update_one({"taskId": "task"}, {"$set": {"runId": "run-new"}})
    before = await repo.db.tasks.find_one({"id": "task"})
    result = await complete_deployment_recovery(repo, "worker-old", "1" * 32)
    assert result["proved"] == result["taskChanged"] == 1
    assert await repo.db.tasks.find_one({"id": "task"}) == before
    assert (await repo.db.endpoint_locks.find_one({"taskId": "task"}))["runId"] == "run-new"


async def test_complete_preserves_already_released_paused_run(tmp_path, monkeypatch):
    """旧Worker已完成暂停收尾清空node时，部署证明不能结束仍需保留预算的run。"""
    repo = repository(tmp_path)
    await insert_owned_run(repo)
    monkeypatch.setattr(audited_mutations, "mutation_transaction", transactionless)
    await prepare_deployment_recovery(repo, "worker-old", token="2" * 32)
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {
        "nodeId": None, "desiredState": "PAUSED", "status": "PAUSED",
    }, "$unset": {"deploymentRecovery": ""}})
    await complete_deployment_recovery(repo, "worker-old", "2" * 32)
    assert not (await repo.db.runs.find_one({"id": "run"})).get("endedAt")
    assert (await repo.db.tasks.find_one({"id": "task"}))["status"] == "PAUSED"
