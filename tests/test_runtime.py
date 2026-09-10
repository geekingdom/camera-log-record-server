"""运行时回调测试：验证实时偏移、状态映射和持久化预算的边界。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection.collector import LogChunk
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs.storage import HourArchive
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


class FakeCollection:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, dict]] = []

    async def update_one(self, query, update, **_kwargs):
        self.calls.append((query, update))


class FakeDatabase:
    def __init__(self) -> None:
        self.files = FakeCollection()
        self.tasks = FakeCollection()


class BudgetCollection:
    def __init__(self) -> None:
        self.values: dict[str, dict] = {}

    async def update_one(self, query, update, **_kwargs):
        entry = self.values.setdefault(query["_id"], {"_id": query["_id"]})
        for key, value in update.get("$setOnInsert", {}).items():
            entry.setdefault(key, value)

    async def find_one_and_update(self, query, update, **_kwargs):
        entry = self.values.get(query["_id"])
        if entry is None or entry.get("attempts", 0) >= query["attempts"]["$lt"]:
            return None
        entry["attempts"] = entry.get("attempts", 0) + update["$inc"]["attempts"]
        return dict(entry)


class InsertCollection:
    def __init__(self) -> None:
        self.values = []

    async def insert_one(self, value, **_kwargs):
        self.values.append(value)


class ReservationTaskCollection:
    """最小任务 CAS 替身，供定时预算测试覆盖事务入口而不依赖 MongoMock。"""
    def __init__(self, task):
        self.task = task

    async def find_one(self, *_args, **_kwargs):
        return dict(self.task)

    async def find_one_and_update(self, _query, update, **_kwargs):
        for key, value in update.get("$inc", {}).items():
            self.task[key] = self.task.get(key, 0) + value
        return dict(self.task)


def runtime_for_callbacks(tmp_path):
    runtime = object.__new__(SessionRuntime)
    runtime.repo = SimpleNamespace(
        settings=SimpleNamespace(log_root=tmp_path, node_id="node-a"), db=FakeDatabase()
    )
    runtime.task = {"id": "task-a", "runId": "run-a"}
    runtime.collector = None
    runtime.frames, runtime.paths = deque(), {}
    runtime.catalog_dirty, runtime.catalog_ready = {}, set()
    runtime.catalog_lock = asyncio.Lock()
    runtime.frame_bytes = runtime.frame_number = runtime.input_bytes = 0
    runtime.last_catalog = 0.0
    runtime.started_at = datetime(2026, 9, 8, tzinfo=UTC)
    runtime.stopping = False
    return runtime


def test_coredump_guard_claims_resource_once_and_rejects_stale_runtime(tmp_path):
    """真实数据库条件保证首次领取、资源健康与运行代次都在发送前复核。"""
    async def scenario():
        database = AsyncMongoMockClient().camera_logs
        settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node-a")
        repo = Repository(database, settings)
        task = {"id": "task-a", "runId": "run-a", "nodeId": "node-a", "generation": 3,
                "resourceId": "resource-a", "desiredState": "RUNNING", "status": "COLLECTING"}
        await database.tasks.insert_one(task)
        await database.resources.insert_one({"id": "resource-a", "deletedAt": None, "healthStatus": "ONLINE"})
        runtime = object.__new__(SessionRuntime)
        runtime.repo, runtime.task = repo, task
        runtime.stopping = runtime.retired = False
        runtime.collector = SimpleNamespace(session_id="session-a", _closed=SimpleNamespace(is_set=lambda: False))
        assert await runtime.coredump_guard() is True
        resource = await database.resources.find_one({"id": "resource-a"})
        assert resource["coredumpLeaseTaskId"] == "task-a" and resource["coredumpLeaseRunId"] == "run-a"
        await database.tasks.update_one({"id": "task-a"}, {"$set": {"generation": 4}})
        assert await runtime.coredump_guard() is False
        await database.tasks.update_one({"id": "task-a"}, {"$set": {"generation": 3}})
        await database.resources.update_one({"id": "resource-a"}, {"$set": {
            "coredumpLeaseTaskId": "task-b", "coredumpLeaseRunId": "run-b"}})
        assert await runtime.coredump_guard() is None
    asyncio.run(scenario())


def test_coredump_guard_rejects_non_collecting_or_unhealthy_resource(tmp_path):
    """停止中的任务和离线资源均不得刷新租约或进入设备控制队列。"""
    async def scenario():
        database = AsyncMongoMockClient().camera_logs
        repo = Repository(database, Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node-a"))
        task = {"id": "task-a", "runId": "run-a", "nodeId": "node-a", "generation": 3,
                "resourceId": "resource-a", "desiredState": "RUNNING", "status": "COLLECTING"}
        await database.tasks.insert_one(task)
        await database.resources.insert_one({"id": "resource-a", "deletedAt": None, "healthStatus": "OFFLINE"})
        runtime = object.__new__(SessionRuntime)
        runtime.repo, runtime.task = repo, task
        runtime.stopping = runtime.retired = False
        runtime.collector = SimpleNamespace(session_id="session-a", _closed=SimpleNamespace(is_set=lambda: False))
        assert await runtime.coredump_guard() is False
        await database.resources.update_one({"id": "resource-a"}, {"$set": {"healthStatus": "ONLINE"}})
        await database.tasks.update_one({"id": "task-a"}, {"$set": {"status": "STOPPING"}})
        assert await runtime.coredump_guard() is False
    asyncio.run(scenario())


def test_coredump_status_report_is_independent_of_log_collection(tmp_path):
    """节点未配置 NFS 时的失败说明只写独立事件和任务字段，不触碰采集状态。"""
    async def scenario():
        events, tasks = InsertCollection(), FakeCollection()
        runtime = object.__new__(SessionRuntime)
        runtime.repo = SimpleNamespace(settings=SimpleNamespace(node_id="node-a"), db=SimpleNamespace(events=events, tasks=tasks))
        runtime.task = {"id": "task-a", "runId": "run-a", "generation": 1, "nodeId": "node-a"}
        runtime.collector = SimpleNamespace(session_id="session-a")

        await runtime.on_coredump("FAILED", "节点未配置 NFS_SERVER_IP")

        assert events.values[0]["status"] == "FAILED"
        assert events.values[0]["error"] == "节点未配置 NFS_SERVER_IP"
        assert tasks.calls[0][1]["$set"]["coredumpMountStatus"] == "FAILED"
    asyncio.run(scenario())


def test_live_chunks_use_their_immutable_path_and_offset(tmp_path):
    runtime = runtime_for_callbacks(tmp_path)
    first = tmp_path / "task-a/run-a/session/2026/09/08/09/part-001.log"
    later = tmp_path / "task-a/run-a/session/2026/09/08/10/part-001.log"
    asyncio.run(runtime.on_log(LogChunk("task-a", "run-a", "session", 1, b"old", 0, str(first))))
    asyncio.run(runtime.on_log(LogChunk("task-a", "run-a", "session", 2, b"new", 0, str(later))))
    frames = list(runtime.frames)
    assert frames[0]["offset"] == 0 and frames[0]["endOffset"] == 3
    assert frames[1]["offset"] == 0 and frames[1]["endOffset"] == 3
    assert frames[0]["fileId"] != frames[1]["fileId"]
    stored_hour = runtime.repo.db.files.calls[0][1]["$set"]["hour"]
    assert stored_hour.endswith("+00:00")


def test_hour_from_path_supports_new_and_legacy_layouts(tmp_path):
    """目录改版只能影响新写入，既有归档的 catalog 小时仍须保持可读。"""
    new_path = tmp_path / "device-a" / "采集任务-full-task-id" / "2026-09-08" / "09" / "part-000001.log"
    legacy_path = tmp_path / "resources" / "device-a" / "full-task-id" / "2026" / "09" / "08" / "09" / "part-000001.log"

    assert SessionRuntime._hour_from_path(new_path) == "2026-09-08T01:00:00+00:00"
    assert SessionRuntime._hour_from_path(legacy_path) == "2026-09-08T01:00:00+00:00"


def test_catalog_flush_publishes_last_quiet_tail_without_next_log(tmp_path):
    """停止输出不足一秒时，目录独立 flush 仍登记最后水位。"""
    runtime = runtime_for_callbacks(tmp_path)
    path = tmp_path / "resources/device/task-a/2026/09/08/09/part-000001.log"
    asyncio.run(runtime.on_log(LogChunk("task-a", "run-a", "session", 1, b"old", 0, str(path))))
    runtime.repo.db.files.calls.clear()
    runtime.last_catalog = time.monotonic()
    asyncio.run(runtime.on_log(LogChunk("task-a", "run-a", "session", 2, b"tail", 3, str(path))))
    assert not runtime.repo.db.files.calls
    asyncio.run(runtime._publish_catalog())
    assert runtime.repo.db.files.calls[-1][1]["$set"]["bytes"] == 7


def test_catalog_flush_publishes_tails_of_multiple_closed_parts(tmp_path):
    """10 MiB 轮转后的旧分卷不能等待当前文件继续写入才登记尾部。"""
    runtime = runtime_for_callbacks(tmp_path)
    first = tmp_path / "resources/device/task-a/2026/09/08/09/part-000001.log"
    second = tmp_path / "resources/device/task-a/2026/09/08/09/part-000002.log"
    async def scenario():
        await runtime.on_log(LogChunk("task-a", "run-a", "session", 1, b"one", 0, str(first)))
        await runtime.on_log(LogChunk("task-a", "run-a", "session", 2, b"two", 0, str(second)))
        runtime.repo.db.files.calls.clear()
        runtime.last_catalog = time.monotonic()
        await runtime.on_log(LogChunk("task-a", "run-a", "session", 3, b"!", 3, str(first)))
        await runtime.on_log(LogChunk("task-a", "run-a", "session", 4, b"?", 3, str(second)))
        await runtime._publish_catalog()
    asyncio.run(scenario())
    assert {call[1]["$set"]["bytes"] for call in runtime.repo.db.files.calls} == {4}


def test_archive_clears_dirty_open_snapshot_before_ready(tmp_path):
    """同一不可变分卷归档后，后台 flush 不得把 READY 回写成 OPEN。"""
    runtime = runtime_for_callbacks(tmp_path)
    path = tmp_path / "resources/device/task-a/2026/09/08/09/part-000001.log"
    archive_path = tmp_path / "archive.tar.gz"
    archive_path.write_bytes(b"archive")
    async def scenario():
        await runtime.on_log(LogChunk("task-a", "run-a", "session", 1, b"tail", 0, str(path)))
        runtime.repo.db.files.calls.clear()
        await runtime.on_archive(HourArchive("task-a", "run-a", "session", datetime(2026, 9, 8, tzinfo=UTC),
            archive_path, 4, "digest", 1, 1, path, path.name, path.with_suffix(".index.jsonl")))
        # 模拟收尾队列中迟到的实时回调；已 READY 的不可变分卷不能重新 OPEN。
        await runtime.on_log(LogChunk("task-a", "run-a", "session", 2, b"late", 4, str(path)))
        await runtime._publish_catalog()
    asyncio.run(scenario())
    assert [call[1]["$set"]["status"] for call in runtime.repo.db.files.calls] == ["READY"]


def test_catalog_publish_keeps_newer_dirty_watermark_created_during_database_await(tmp_path):
    """DB 写入让出事件循环时，后续块替换的 dirty 水位必须留给下一次发布。"""
    runtime = runtime_for_callbacks(tmp_path)
    path = tmp_path / "resources/device/task-a/2026/09/08/09/part-000001.log"

    class RacingFiles(FakeCollection):
        async def update_one(self, query, update, **kwargs):
            self.calls.append((query, update))
            if len(self.calls) == 1:
                runtime.last_catalog = time.monotonic()
                await runtime.on_log(LogChunk("task-a", "run-a", "session", 2, b"tail", 3, str(path)))

    async def scenario():
        await runtime.on_log(LogChunk("task-a", "run-a", "session", 1, b"old", 0, str(path)))
        runtime.repo.db.files = RacingFiles()
        runtime.last_catalog = time.monotonic()
        await runtime.on_log(LogChunk("task-a", "run-a", "session", 1, b"old", 0, str(path)))
        await runtime._publish_catalog()
        assert runtime.catalog_dirty
        await runtime._publish_catalog()

    asyncio.run(scenario())
    assert runtime.repo.db.files.calls[-1][1]["$set"]["bytes"] == 7


def test_catalog_open_and_ready_failures_remain_dirty_and_retry(tmp_path):
    """OPEN 与 READY 任一首次落库失败都保留条目，后续发布可恢复。"""
    runtime = runtime_for_callbacks(tmp_path)
    path = tmp_path / "resources/device/task-a/2026/09/08/09/part-000001.log"
    archive_path = tmp_path / "archive.tar.gz"
    archive_path.write_bytes(b"archive")

    class FailOnceFiles(FakeCollection):
        failed = False

        async def update_one(self, query, update, **kwargs):
            if not self.failed:
                self.failed = True
                self.calls.append((query, update))
                raise OSError("temporary mongo failure")
            return await super().update_one(query, update, **kwargs)

    async def scenario():
        runtime.repo.db.files = FailOnceFiles()
        with pytest.raises(OSError, match="temporary mongo failure"):
            await runtime.on_log(LogChunk("task-a", "run-a", "session", 1, b"open", 0, str(path)))
        assert runtime.catalog_dirty
        await runtime._publish_catalog()
        original = runtime.repo.db.files.update_one
        failed = False
        async def fail_ready(query, update, **kwargs):
            nonlocal failed
            if update["$set"]["status"] == "READY" and not failed:
                failed = True
                raise OSError("ready mongo failure")
            return await original(query, update, **kwargs)
        runtime.repo.db.files.update_one = fail_ready
        archive = HourArchive("task-a", "run-a", "session", datetime(2026, 9, 8, tzinfo=UTC),
            archive_path, 4, "digest", 1, 1, path, path.name, path.with_suffix(".index.jsonl"))
        with pytest.raises(OSError, match="ready mongo failure"):
            await runtime.on_archive(archive)
        assert runtime.catalog_dirty
        await runtime._publish_catalog()
        assert not runtime.catalog_dirty

    asyncio.run(scenario())


def test_catalog_loop_flushes_dirty_entry_without_new_log(tmp_path, monkeypatch):
    """后台循环本身驱动尾部发布，测试以受控 sleep 避免依赖墙钟。"""
    runtime = runtime_for_callbacks(tmp_path)
    path = tmp_path / "resources/device/task-a/2026/09/08/09/part-000001.log"
    async def scenario():
        runtime.last_catalog = time.monotonic()
        runtime.paths[runtime.file_id(path)] = 1
        await runtime.on_log(LogChunk("task-a", "run-a", "session", 1, b"tail", 1, str(path)))
        assert runtime.catalog_dirty
        async def tick(_seconds):
            runtime.stopping = True
        monkeypatch.setattr("camera_logs.collection.runtime.asyncio.sleep", tick)
        await runtime._catalog_loop()
    asyncio.run(scenario())
    assert runtime.repo.db.files.calls[-1][1]["$set"]["bytes"] == 5


def test_runtime_stop_retries_catalog_after_collector_stop_error_and_rethrows(tmp_path):
    """采集器归档回调失败后，stop 仍冲刷目录、取消后台任务并把错误交给 Worker。"""
    async def scenario():
        runtime = runtime_for_callbacks(tmp_path)
        runtime.catalog_task = asyncio.create_task(asyncio.sleep(10))
        runtime.background = asyncio.create_task(asyncio.sleep(10))
        async def fail_collector_stop():
            raise OSError("collector archive callback failed")
        runtime.collector = SimpleNamespace(stop=fail_collector_stop)
        runtime.catalog_dirty["ready"] = {"id": "ready", "taskId": "task-a", "runId": "run-a",
            "sessionId": "session", "nodeId": "node-a", "hour": "2026-09-08T01:00:00+00:00",
            "path": str(tmp_path / "hour.tar.gz"), "archiveName": "hour.tar.gz", "bytes": 4,
            "archiveBytes": 10, "sha256": "digest", "firstSequence": 1, "lastSequence": 1,
            "status": "READY", "runStartedAt": runtime.started_at, "sessionStartedAt": runtime.started_at}
        with pytest.raises(OSError, match="collector archive callback failed"):
            await runtime.stop()
        return runtime

    runtime = asyncio.run(scenario())
    assert runtime.repo.db.files.calls[-1][1]["$set"]["status"] == "READY"
    assert runtime.catalog_task.cancelled() and runtime.background.cancelled()


def test_runtime_stop_rethrows_final_catalog_failure_after_cancelling_tasks(tmp_path):
    """最终目录写入失败不能被吞掉，且后台任务仍必须释放。"""
    class FailingFiles(FakeCollection):
        async def update_one(self, *_args, **_kwargs):
            raise OSError("final catalog write failed")

    async def scenario():
        runtime = runtime_for_callbacks(tmp_path)
        runtime.repo.db.files = FailingFiles()
        runtime.catalog_task = asyncio.create_task(asyncio.sleep(10))
        runtime.background = asyncio.create_task(asyncio.sleep(10))
        runtime.catalog_dirty["open"] = {"id": "open", "taskId": "task-a", "runId": "run-a",
            "sessionId": "session", "nodeId": "node-a", "hour": "2026-09-08T01:00:00+00:00",
            "path": str(tmp_path / "part.log"), "bytes": 4, "status": "OPEN", "firstSequence": 1,
            "runStartedAt": runtime.started_at, "sessionStartedAt": runtime.started_at}
        with pytest.raises(OSError, match="final catalog write failed"):
            await runtime.stop()
        return runtime

    runtime = asyncio.run(scenario())
    assert runtime.catalog_dirty
    assert runtime.catalog_task.cancelled() and runtime.background.cancelled()


def test_closed_reconnects_while_archive_failure_preserves_collecting_status(tmp_path):
    async def scenario():
        runtime = runtime_for_callbacks(tmp_path)
        await runtime.on_state("CLOSED", {"sessionId": "old"})
        await runtime.on_state("ARCHIVE_ERROR", {"sessionId": "old", "error": "full"})
        return runtime.repo.db.tasks.calls

    calls = asyncio.run(scenario())
    assert calls[0][1]["$set"]["status"] == "RECONNECTING"
    assert "status" not in calls[1][1]["$set"]
    assert calls[1][1]["$set"]["archiveError"] == "full"


def test_transport_error_states_are_exposed_as_reconnecting(tmp_path):
    async def scenario():
        runtime = runtime_for_callbacks(tmp_path)
        await runtime.on_state("READ_ERROR", {"sessionId": "old"})
        await runtime.on_state("IDLE_TIMEOUT", {"sessionId": "old"})
        return runtime.repo.db.tasks.calls

    calls = asyncio.run(scenario())
    assert [call[1]["$set"]["status"] for call in calls] == ["RECONNECTING", "RECONNECTING"]


def test_scheduled_budget_is_cumulative_across_reconnected_sessions(tmp_path, mock_reservation_transaction):
    async def scenario():
        runtime = runtime_for_callbacks(tmp_path)
        runtime.task["scheduledCommands"] = [{"id": "repeat", "totalExecutions": 2}]
        runtime.collector = SimpleNamespace(session_id="session-one")
        runtime.pending_executions = {}
        runtime.repo.db.budgets = BudgetCollection()
        runtime.repo.db.commands = InsertCollection()
        runtime.repo.db.tasks = ReservationTaskCollection(runtime.task)
        detail = {"taskId": "task-a", "runId": "run-a"}
        first = await runtime.reserve("repeat", detail | {"sessionId": "session-one", "execution": 1})
        runtime.collector = SimpleNamespace(session_id="session-two")
        second = await runtime.reserve("repeat", detail | {"sessionId": "session-two", "execution": 2})
        third = await runtime.reserve("repeat", detail | {"sessionId": "session-two", "execution": 3})
        return first, second, third, runtime.repo.db.commands.values

    first, second, third, executions = asyncio.run(scenario())
    assert (first, second, third) == (True, True, False)
    assert [record["attempt"] for record in executions] == [1, 2]
    assert [record["sessionId"] for record in executions] == ["session-one", "session-two"]


async def test_default_idle_timeout_closes_before_reconnect_and_replays_initial_commands(tmp_path, monkeypatch):
    """默认十秒无设备输出时，旧会话关闭完成后才创建新会话并重放初始化命令。"""
    class Connection:
        def __init__(self, number, order):
            self.number, self.order = number, order
            self.created_at = time.monotonic()
            self.received = asyncio.Queue()
            self.sent = []
            self.close_count = 0
            self.closed_at = None
            self.initial_sent = asyncio.Event()

        async def read(self, _size=65536):
            value = await self.received.get()
            return value or b""

        async def write(self, data):
            self.sent.append(data)
            if data == b"initialise\n":
                self.initial_sent.set()

        async def close(self):
            self.close_count += 1
            self.closed_at = time.monotonic()
            self.order.append(f"close-{self.number}")
            self.received.put_nowait(None)

    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                        node_id="idle-node", start_background=False)
    repo = Repository(AsyncMongoMockClient().camera_logs, settings)
    await repo.initialize()
    task = {
        "id": "idle-task", "runId": "idle-run", "nodeId": "idle-node", "generation": 1,
        "status": "PENDING", "desiredState": "RUNNING", "protocol": "SSH", "ip": "192.0.2.44",
        "port": 22, "passwordEncrypted": repo.encrypt(""), "storageIdentity": "idledevice",
        "initialCommands": [{"command": "initialise"}], "scheduledCommands": [],
    }
    await repo.db.tasks.insert_one(task.copy())
    order, connections, reconnected = [], [], asyncio.Event()

    async def factory(_task):
        number = len(connections) + 1
        order.append(f"factory-{number}")
        connection = Connection(number, order)
        connections.append(connection)
        if number == 2:
            reconnected.set()
        return connection

    # 保留生产默认 10 秒空闲阈值，仅消除重连抖动使测试时间上界稳定。
    monkeypatch.setattr("random.random", lambda: 0)
    runtime = SessionRuntime(repo, task, connection_factory=AsyncMock(side_effect=factory))
    observed, collecting_twice = [], asyncio.Event()
    original_on_state = runtime.on_state

    async def on_state(state, details):
        observed.append((state, details.get("sessionId")))
        await original_on_state(state, details)
        if state == "COLLECTING" and len([item for item in observed if item[0] == "COLLECTING"]) == 2:
            collecting_twice.set()

    runtime.on_state = on_state
    try:
        await asyncio.wait_for(reconnected.wait(), timeout=13)
        await asyncio.wait_for(connections[1].initial_sent.wait(), timeout=1)
        await asyncio.wait_for(collecting_twice.wait(), timeout=1)
        collecting_sessions = [session_id for state, session_id in observed if state == "COLLECTING"]
        events = [item async for item in repo.db.events.find({"taskId": task["id"]})]
        assert order[:3] == ["factory-1", "close-1", "factory-2"]
        assert connections[0].close_count == 1
        assert connections[0].closed_at - connections[0].created_at >= 10
        assert [connection.sent for connection in connections] == [[b"initialise\n"], [b"initialise\n"]]
        assert len(collecting_sessions) == 2 and len(set(collecting_sessions)) == 2
        assert "IDLE_TIMEOUT" in [state for state, _session_id in observed]
        assert "CONNECTION_GAP" in [event["type"] for event in events]
    finally:
        await runtime.stop()
    assert connections[1].close_count == 1
    assert len(connections) == 2


def test_cancelled_runtime_task_does_not_raise_while_worker_releases_it(tmp_path):
    async def scenario():
        task = asyncio.create_task(asyncio.sleep(10))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        runtime = runtime_for_callbacks(tmp_path)
        runtime.background = task
        return runtime.background_failure()

    assert asyncio.run(scenario()) is None
