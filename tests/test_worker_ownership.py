"""节点收尾归属测试：过期实例必须释放连接而不能结束后继运行。"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
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


async def test_blocked_stop_with_active_owner_releases_run_lock_and_stop_operation(tmp_path):
    """BLOCKED 后普通停止仍由同 owner runtime 完成关闭、收据、锁和操作收尾。"""
    repo = isolated_repository(tmp_path)
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1,
            "sessionId": "session", "status": "BLOCKED", "desiredState": "STOPPED",
            "restartRequested": False}
    await repo.db.tasks.insert_one(task)
    await repo.db.runs.insert_one({"id": "run", "taskId": "task"})
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "run"})
    await repo.db.operations.insert_one({"id": "stop", "taskId": "task", "desiredState": "STOPPED",
                                         "status": "PENDING", "action": "stop"})
    runtime = SimpleNamespace(task=task, collector=SimpleNamespace(session_id="session"), stop=AsyncMock(),
                              error=None, input_bytes=0, background_failure=lambda: None)
    worker = Worker(repo)
    worker.active["task"] = runtime
    worker.last_coredump_scan = worker.last_maintenance = time.monotonic()

    await worker.tick()
    await worker.releases["task"]

    stored = await repo.db.tasks.find_one({"id": "task"})
    assert runtime.stop.await_count == 1
    assert stored["status"] == "STOPPED" and stored["nodeId"] is None
    assert stored["closedReceipt"]["sessionId"] == "session"
    assert await repo.db.endpoint_locks.count_documents({"taskId": "task"}) == 0
    assert (await repo.db.runs.find_one({"id": "run"}))["endedAt"]
    assert (await repo.db.operations.find_one({"id": "stop"}))["status"] == "SUCCEEDED"


async def test_blocked_restart_with_active_owner_ignores_old_runtime_error_after_confirmed_stop(tmp_path):
    """用户已请求恢复时，旧会话历史错误不能覆盖成功关闭后的新运行意图。"""
    repo = isolated_repository(tmp_path)
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1,
            "sessionId": "session", "resourceId": "resource", "status": "BLOCKED",
            "desiredState": "STOPPED", "restartRequested": True, "controlOperationId": "restart"}
    await repo.db.tasks.insert_one(task)
    await repo.db.resources.insert_one({"id": "resource", "deletedAt": None, "healthStatus": "ONLINE"})
    await repo.db.runs.insert_one({"id": "run", "taskId": "task"})
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "run"})
    await repo.db.operations.insert_one({"id": "restart", "taskId": "task", "desiredState": "RUNNING",
                                         "status": "PENDING", "action": "restart-blocked"})
    runtime = SimpleNamespace(task=task, collector=SimpleNamespace(session_id="session"), stop=AsyncMock(),
                              error="旧连接错误", input_bytes=0, background_failure=lambda: None)
    worker = Worker(repo)
    worker.active["task"] = runtime
    worker.last_coredump_scan = worker.last_maintenance = time.monotonic()

    await worker.tick()
    await worker.releases["task"]

    stored = await repo.db.tasks.find_one({"id": "task"})
    assert runtime.stop.await_count == 1
    assert (stored["status"], stored["desiredState"], stored["nodeId"], stored["restartRequested"]) == (
        "STOPPED", "RUNNING", None, False
    )
    assert await repo.db.endpoint_locks.count_documents({"taskId": "task"}) == 0
    assert (await repo.db.operations.find_one({"id": "restart"}))["status"] == "PENDING"


async def test_blocked_stop_without_runtime_marks_isolation_required_without_releasing(tmp_path):
    """未知旧运行不能因普通停止假定关闭，操作需明确等待单任务隔离确认。"""
    repo = isolated_repository(tmp_path)
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1,
            "status": "BLOCKED", "desiredState": "STOPPED", "restartRequested": False}
    await repo.db.tasks.insert_one(task)
    await repo.db.runs.insert_one({"id": "run", "taskId": "task"})
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "run"})
    await repo.db.operations.insert_one({"id": "stop", "taskId": "task", "desiredState": "STOPPED",
                                         "status": "PENDING", "action": "stop"})
    worker = Worker(repo)
    worker.last_coredump_scan = worker.last_maintenance = time.monotonic()

    await worker.tick()

    assert (await repo.db.tasks.find_one({"id": "task"}))["status"] == "BLOCKED"
    assert await repo.db.endpoint_locks.count_documents({"taskId": "task", "runId": "run"}) == 1
    assert (await repo.db.runs.find_one({"id": "run"})).get("endedAt") is None
    assert (await repo.db.operations.find_one({"id": "stop"}))["phase"] == "ISOLATION_REQUIRED"


async def test_blocked_stop_without_runtime_consumes_exact_closed_receipt(tmp_path):
    """本机已丢失 runtime 时，精确关闭收据仍可完成普通停止的事务收尾。"""
    repo = isolated_repository(tmp_path)
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1,
            "sessionId": "session", "status": "BLOCKED", "desiredState": "STOPPED",
            "restartRequested": False, "controlOperationId": "stop"}
    receipt = {"taskId": "task", "runId": "run", "nodeId": "node", "generation": 1,
               "sessionId": "session", "instanceId": "worker", "closedAt": now()}
    await repo.db.tasks.insert_one(task | {"closedReceipt": receipt})
    await repo.db.runs.insert_one({"id": "run", "taskId": "task"})
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "run"})
    await repo.db.commands.insert_many([
        {"taskId": "task", "runId": "run", "status": "SENDING"},
        {"taskId": "task", "runId": "run", "status": "QUEUED"},
    ])
    await repo.db.operations.insert_one({"id": "stop", "taskId": "task", "desiredState": "STOPPED",
                                         "status": "PENDING", "action": "stop"})
    worker = Worker(repo)
    worker.last_coredump_scan = worker.last_maintenance = time.monotonic()

    await worker.tick()

    stored = await repo.db.tasks.find_one({"id": "task"})
    assert (stored["status"], stored["desiredState"], stored["nodeId"]) == ("STOPPED", "STOPPED", None)
    assert await repo.db.endpoint_locks.count_documents({"taskId": "task"}) == 0
    assert (await repo.db.runs.find_one({"id": "run"}))["endedAt"]
    assert [entry["status"] async for entry in repo.db.commands.find({"taskId": "task"}).sort("status", 1)] == [
        "CANCELLED", "UNKNOWN",
    ]
    operation = await repo.db.operations.find_one({"id": "stop"})
    assert operation["status"] == "SUCCEEDED" and "phase" not in operation


async def test_blocked_restart_without_runtime_consumes_receipt_but_stays_pending(tmp_path):
    """收据可释放旧运行并重新排队，但 restart operation 必须等待新会话采集。"""
    repo = isolated_repository(tmp_path)
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1,
            "sessionId": "session", "status": "BLOCKED", "desiredState": "STOPPED",
            "restartRequested": True, "controlOperationId": "restart"}
    receipt = {"taskId": "task", "runId": "run", "nodeId": "node", "generation": 1,
               "sessionId": "session", "instanceId": "worker", "closedAt": now()}
    await repo.db.tasks.insert_one(task | {"closedReceipt": receipt})
    await repo.db.runs.insert_one({"id": "run", "taskId": "task"})
    await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "run"})
    await repo.db.operations.insert_one({"id": "restart", "taskId": "task", "desiredState": "RUNNING",
                                         "status": "PENDING", "action": "restart-blocked"})
    await repo.db.resources.insert_one({"id": "resource", "deletedAt": None, "healthStatus": "ONLINE"})
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {"resourceId": "resource"}})
    worker = Worker(repo)
    worker.last_coredump_scan = worker.last_maintenance = time.monotonic()

    await worker.tick()

    stored = await repo.db.tasks.find_one({"id": "task"})
    assert (stored["status"], stored["desiredState"], stored["nodeId"], stored["restartRequested"]) == (
        "STOPPED", "RUNNING", None, False
    )
    assert await repo.db.endpoint_locks.count_documents({"taskId": "task"}) == 0
    operation = await repo.db.operations.find_one({"id": "restart"})
    assert operation["status"] == "PENDING" and "phase" not in operation
