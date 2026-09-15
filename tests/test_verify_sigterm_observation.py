"""SIGTERM验收不能要求调度器维持瞬时STOPPED状态。"""

import importlib

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


@pytest.fixture
def verifier(monkeypatch):
    """从真实脚本加载状态判定，不复制被测逻辑。"""
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_worker_sigterm_resume")


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_scheduler_pending_after_release_is_valid_observation(tmp_path, verifier):
    """模拟Worker已退出且调度先于观察轮询执行，必须接受正常排队状态。"""
    repo = Repository(AsyncMongoMockClient().db, Settings(
        _env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))
    await repo.initialize()
    await repo.db.tasks.insert_one({"id": "task", "runId": "run", "generation": 1,
        "nodeId": None, "status": "STOPPED", "desiredState": "RUNNING"})
    await schedule_once(repo)
    task = await repo.db.tasks.find_one({"id": "task"})
    assert task["status"] == "PENDING"
    assert verifier.awaiting_replacement(task)


@pytest.mark.parametrize("change", [
    {"status": "BLOCKED"}, {"status": "ERROR"}, {"status": "STOPPING"},
    {"desiredState": "STOPPED"}, {"desiredState": "PAUSED"}, {"nodeId": "old-worker"},
])
def test_incomplete_or_cancelled_release_is_rejected(verifier, change):
    """排队兼容不得把隔离、未关闭或用户停止当成可恢复收尾。"""
    assert not verifier.awaiting_replacement(
        {"status": "STOPPED", "desiredState": "RUNNING", "nodeId": None} | change)
