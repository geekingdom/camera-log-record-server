"""运行时回调测试：验证实时偏移、状态映射和持久化预算的边界。"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from camera_logs.collection.collector import LogChunk
from camera_logs.collection.runtime import SessionRuntime


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

    async def insert_one(self, value):
        self.values.append(value)


def runtime_for_callbacks(tmp_path):
    runtime = object.__new__(SessionRuntime)
    runtime.repo = SimpleNamespace(
        settings=SimpleNamespace(log_root=tmp_path, node_id="node-a"), db=FakeDatabase()
    )
    runtime.task = {"id": "task-a", "runId": "run-a"}
    runtime.collector = None
    runtime.frames, runtime.paths = deque(), {}
    runtime.frame_bytes = runtime.frame_number = runtime.input_bytes = 0
    runtime.last_catalog = 0.0
    runtime.started_at = datetime(2026, 9, 8, tzinfo=UTC)
    runtime.stopping = False
    return runtime


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


def test_scheduled_budget_is_cumulative_across_reconnected_sessions(tmp_path):
    async def scenario():
        runtime = runtime_for_callbacks(tmp_path)
        runtime.task["scheduledCommands"] = [{"id": "repeat", "totalExecutions": 2}]
        runtime.collector = SimpleNamespace(session_id="session-one")
        runtime.pending_executions = {}
        runtime.repo.db.budgets = BudgetCollection()
        runtime.repo.db.commands = InsertCollection()
        runtime.repo.db.tasks.find_one = AsyncMock(return_value=runtime.task)
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


def test_cancelled_runtime_task_does_not_raise_while_worker_releases_it(tmp_path):
    async def scenario():
        task = asyncio.create_task(asyncio.sleep(10))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        runtime = runtime_for_callbacks(tmp_path)
        runtime.background = task
        return runtime.background_failure()

    assert asyncio.run(scenario()) is None
