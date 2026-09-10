"""节点收尾归属测试：过期实例必须释放连接而不能结束后继运行。"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.worker import Worker
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


def isolated_repository(tmp_path, node_id="node"):
    """测试生成独立密钥且禁用本机环境文件，保持无凭据 CI 与开发机行为一致。"""
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(),
                        log_root=tmp_path, node_id=node_id)
    return Repository(AsyncMongoMockClient().db, settings)


@pytest.mark.parametrize("method", ["release", "pause", "pause_pending", "stop_pending"])
@pytest.mark.parametrize("changed_field", ["generation", "nodeId", "runId"])
async def test_stale_owner_cannot_change_successor_records(tmp_path, method, changed_field):
    """即使任务名与运行仍相同，代次或节点变化也使旧快照失效。"""
    repo = isolated_repository(tmp_path, "node-old")
    worker = Worker(repo)
    old = {"id": "task", "runId": "run", "nodeId": "node-old", "generation": 1,
           "status": "PENDING", "desiredState": "PAUSED" if "pause" in method else "STOPPED"}
    current = old | {changed_field: {"generation": 2, "nodeId": "node-new", "runId": "new-run"}[changed_field],
                     "restartRequested": True}
    await repo.db.tasks.insert_one(current)
    await repo.db.runs.insert_one({"id": current["runId"], "generation": current["generation"]})
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": current["runId"], "endpoint": "host:22"})
    await repo.db.operations.insert_one({"id": "new-operation", "taskId": "task",
                                         "desiredState": current["desiredState"], "status": "PENDING"})
    collections = ("tasks", "runs", "endpoint_locks", "operations", "events")
    before = {name: [item async for item in repo.db[name].find({})] for name in collections}
    runtime = SimpleNamespace(task=old, stop=AsyncMock(), error=None, background_failure=lambda: None)
    successor = object()
    worker.active["task"] = successor

    await getattr(worker, method)(old if method.endswith("pending") else runtime)

    if not method.endswith("pending"):
        assert runtime.stop.await_count >= 1
    after = {name: [item async for item in repo.db[name].find({})] for name in collections}
    assert after == before
    assert worker.active["task"] is successor


async def test_stale_release_removes_only_its_own_closed_instance(tmp_path):
    """数据库归属已变化但本机仍挂着旧实例时，成功关闭后从本机容器移除。"""
    repo = isolated_repository(tmp_path)
    old = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1}
    await repo.db.tasks.insert_one(old | {"generation": 2, "status": "COLLECTING"})
    runtime = SimpleNamespace(task=old, stop=AsyncMock(), error=None, background_failure=lambda: None)
    worker = Worker(repo)
    worker.active["task"] = runtime

    await worker.release(runtime)

    runtime.stop.assert_awaited_once()
    assert "task" not in worker.active
    assert (await repo.db.tasks.find_one({"id": "task"}))["status"] == "COLLECTING"


@pytest.mark.parametrize("failed", [False, True])
async def test_edit_restart_only_resumes_after_successful_release(tmp_path, failed):
    """编辑重启在旧运行成功收尾后恢复；已知故障保持停止并报告操作失败。"""
    repo = isolated_repository(tmp_path)
    task = {"id": "edited", "runId": "old-run", "nodeId": "node", "generation": 1,
            "desiredState": "STOPPED", "status": "COLLECTING", "restartRequested": True}
    await repo.db.tasks.insert_one(task)
    await repo.db.runs.insert_one({"id": "old-run"})
    await repo.db.endpoint_locks.insert_one({"taskId": "edited", "runId": "old-run"})
    await repo.db.operations.insert_one({"id": "edit-stop", "taskId": "edited", "desiredState": "STOPPED",
                                         "status": "PENDING", "action": "edit-stop"})
    runtime = SimpleNamespace(task=task, stop=AsyncMock(), error="storage error" if failed else None,
                              background_failure=lambda: None)
    await Worker(repo).release(runtime)
    result = await repo.get("tasks", "edited")
    assert result["desiredState"] == ("STOPPED" if failed else "RUNNING")
    assert result["status"] == ("ERROR" if failed else "STOPPED")
    assert result["restartRequested"] is False
    assert (await repo.get("operations", "edit-stop"))["status"] == ("FAILED" if failed else "SUCCEEDED")
    assert (await repo.get("runs", "old-run"))["endedAt"]
    assert await repo.db.endpoint_locks.count_documents({"taskId": "edited"}) == 0


@pytest.mark.parametrize("state", ["CLOSED", "COLLECTING", "ARCHIVE_ERROR", "DEBUG"])
async def test_old_generation_callbacks_preserve_current_task_and_operation(tmp_path, state):
    """真实运行时回调也必须遵守领取代次，不能在 Worker 条件更新前覆盖后继。"""
    repo = isolated_repository(tmp_path)
    old = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1}
    current = old | {"generation": 2, "status": "COLLECTING", "sessionId": "new-session"}
    await repo.db.tasks.insert_one(current)
    await repo.db.operations.insert_one({"id": "start", "taskId": "task", "desiredState": "RUNNING", "status": "PENDING"})
    runtime = object.__new__(SessionRuntime)
    runtime.task, runtime.repo, runtime.stopping = old, repo, True
    runtime.collector = SimpleNamespace(session_id="old-session")

    if state == "DEBUG":
        await runtime.on_debug("FAILED", {"mode": "PSH", "commandBlocked": True})
    else:
        await runtime.on_state(state, {"sessionId": "old-session", "error": "old archive error"})

    assert await repo.db.tasks.find_one({"id": "task"}) == current
    assert (await repo.db.operations.find_one({"id": "start"}))["status"] == "PENDING"


async def test_release_database_failure_preserves_owner_until_retry(tmp_path, monkeypatch):
    """关闭连接后数据库清理失败，保留领取身份，重试可完成剩余收尾。"""
    repo = isolated_repository(tmp_path)
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1,
            "desiredState": "STOPPED", "status": "COLLECTING"}
    await repo.db.tasks.insert_one(task)
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "run"})
    runtime = SimpleNamespace(task=task, stop=AsyncMock(), error=None, background_failure=lambda: None)
    worker = Worker(repo)
    worker.active["task"] = runtime
    collection_type = type(repo.db.endpoint_locks)
    original = collection_type.delete_one
    monkeypatch.setattr(collection_type, "delete_one", AsyncMock(side_effect=OSError("database unavailable")))

    with pytest.raises(OSError, match="database unavailable"):
        await worker.release(runtime)

    stored = await repo.db.tasks.find_one({"id": "task"})
    assert stored["nodeId"] == "node" and stored["status"] == "STOPPING"
    assert worker.active["task"] is runtime
    monkeypatch.setattr(collection_type, "delete_one", original)
    await worker.release(runtime)
    stored = await repo.db.tasks.find_one({"id": "task"})
    assert stored["nodeId"] is None and stored["status"] == "STOPPED"
    assert await repo.db.endpoint_locks.count_documents({}) == 0
    assert "task" not in worker.active


async def test_worker_retries_its_own_blocked_release_after_connection_closed(tmp_path):
    """收尾首次报错即使连接已关闭，也只允许原 Worker 以相同归属补齐释放。"""
    repo = isolated_repository(tmp_path)
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1,
            "desiredState": "STOPPED", "status": "COLLECTING"}
    await repo.db.tasks.insert_one(task)
    await repo.db.runs.insert_one({"id": "run"})
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "run"})
    await repo.db.operations.insert_one({"id": "system-stop", "taskId": "task", "desiredState": "STOPPED",
                                         "status": "PENDING"})

    class ClosedButUnreportedRuntime:
        """首次关闭已完成传输层动作，但报告异常模拟日志收尾失败。"""
        def __init__(self):
            self.task = task
            self.closed = False
            self.attempts = 0
            self.error = None
            self.input_bytes = 0

        async def stop(self):
            self.attempts += 1
            self.closed = True
            if self.attempts == 1:
                raise OSError("日志关闭结果未确认")

        def background_failure(self):
            return None

    runtime = ClosedButUnreportedRuntime()
    worker = Worker(repo)
    worker.active[task["id"]] = runtime

    with pytest.raises(OSError, match="日志关闭结果未确认"):
        await worker.finish_runtime(runtime, "release")

    blocked = await repo.db.tasks.find_one({"id": "task"})
    assert runtime.closed and blocked["status"] == "BLOCKED"
    assert blocked["nodeId"] == "node"
    assert await repo.db.endpoint_locks.count_documents({"taskId": "task"}) == 1
    assert (await repo.db.operations.find_one({"id": "system-stop"}))["status"] == "FAILED"

    worker.last_coredump_scan = time.monotonic()
    worker.last_maintenance = time.monotonic()
    await worker.tick()
    await worker.releases[task["id"]]

    released = await repo.db.tasks.find_one({"id": "task"})
    assert released["status"] == "STOPPED" and released["nodeId"] is None
    assert (await repo.db.runs.find_one({"id": "run"}))["endedAt"]
    assert await repo.db.endpoint_locks.count_documents({"taskId": "task"}) == 0
    # 原控制请求已在首次关闭异常时失败，后台补齐资源收尾不得回写为成功。
    assert (await repo.db.operations.find_one({"id": "system-stop"}))["status"] == "FAILED"
    assert "task" not in worker.active


async def test_blocked_cleanup_retry_does_not_release_successor_owner(tmp_path):
    """已记录的本机失败收尾在归属变更后只能丢弃，不能删除后继锁。"""
    repo = isolated_repository(tmp_path)
    old = {"id": "task", "runId": "old-run", "nodeId": "node-old", "generation": 1,
           "desiredState": "STOPPED", "status": "COLLECTING"}
    await repo.db.tasks.insert_one(old)
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "old-run"})

    class FailingRuntime:
        """模拟关闭后首次报告失败的旧实例。"""
        def __init__(self):
            self.task = old
            self.error = None

        async def stop(self):
            raise OSError("旧实例关闭报告失败")

        def background_failure(self):
            return None

    runtime = FailingRuntime()
    worker = Worker(repo)
    worker.active["task"] = runtime
    with pytest.raises(OSError, match="旧实例关闭报告失败"):
        await worker.finish_runtime(runtime, "release")

    successor = old | {"runId": "new-run", "nodeId": "node-new", "generation": 2, "status": "BLOCKED"}
    await repo.db.tasks.replace_one({"id": "task"}, successor)
    await repo.db.endpoint_locks.delete_many({"taskId": "task"})
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "new-run"})

    assert not await worker.retry_blocked_cleanup(runtime)
    assert await repo.db.tasks.find_one({"id": "task"}) == successor
    assert await repo.db.endpoint_locks.find_one({"taskId": "task", "runId": "new-run"}) is not None
