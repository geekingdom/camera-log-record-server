"""写压诊断必须有界、可关联最慢会话，并且不改变既有准入含义。"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.worker import Worker
from camera_logs.node.write_diagnostics import EventLoopWatchdog, WritePressureDiagnostics
from camera_logs.node.write_pressure import write_admission
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


def diagnostic(task="task-a"):
    return {"task": {"taskId": task, "runId": "run-a", "sessionId": "session-a"},
            "endToEnd": {"p99Ms": 800, "pendingMs": 900, "samples": 3},
            "io": {"p99Ms": 100, "pendingMs": 0, "samples": 2},
            "buffer": {"reservedBytes": 1024, "limitBytes": 4096, "oldestPendingMs": 900},
            "writeExecutor": {"concurrency": 4, "active": 4, "waiting": 12,
                              "queue": {"p99Ms": 700}, "execution": {"p99Ms": 100}},
            "eventLoop": {"p99Ms": 12, "samples": 4, "intervalMs": 250}}


def test_write_pressure_diagnostic_logs_change_sustained_and_recovery_without_batch_noise(caplog):
    tick = [0.0]
    reporter = WritePressureDiagnostics(clock=lambda: tick[0])
    metrics = {"writeLatencyMs": 900}
    caplog.set_level(logging.INFO)

    assert reporter.report("node-a", metrics, 200, diagnostic()) == "CHANGED"
    assert reporter.report("node-a", metrics, 200, diagnostic()) is None
    tick[0] = 29.9
    assert reporter.report("node-a", metrics, 200, diagnostic()) is None
    tick[0] = 30.0
    assert reporter.report("node-a", metrics, 200, diagnostic()) == "SUSTAINED"
    metrics["writeLatencyMs"] = 200
    assert reporter.report("node-a", metrics, 200, diagnostic()) == "RECOVERED"

    records = [record.context for record in caplog.records if getattr(record, "context", {}).get("type") == "WRITE_PRESSURE_DIAGNOSTIC"]
    assert [record["phase"] for record in records] == ["CHANGED", "SUSTAINED", "RECOVERED"]
    assert all(set(record) == {"type", "phase", "level", "nodeId", "taskId", "runId", "sessionId", "limitMs", "admission", "endToEnd", "io", "buffer", "writeExecutor", "eventLoop"} for record in records)
    assert records[0]["taskId"] == "task-a" and records[0]["writeExecutor"]["waiting"] == 12


@pytest.mark.asyncio
async def test_event_loop_watchdog_samples_lag_with_bounded_memory_and_closes():
    watchdog = EventLoopWatchdog(interval_seconds=.001, sample_limit=2)
    watchdog.start()
    await asyncio.sleep(.008)
    snapshot = watchdog.snapshot()
    assert 1 <= snapshot["samples"] <= 2 and snapshot["intervalMs"] == 1
    await watchdog.close()
    assert watchdog._task is None


@pytest.mark.parametrize("task_count", [83, 100])
async def test_worker_diagnostic_identifies_one_slow_session_under_83_or_100_task_injection(tmp_path, monkeypatch, task_count):
    """大批会话中一条慢写仍可定位，诊断不能因任务数丢失共享执行器排队信息。"""
    repo = Repository(AsyncMongoMockClient().db, Settings(
        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path))
    worker = Worker(repo)
    normal = {"p99Ms": 2.0, "samples": 1, "pendingMs": 0.0, "windowSeconds": 60}
    slow = {"p99Ms": 17_700.0, "samples": 1, "pendingMs": 3_754.0, "windowSeconds": 60}
    worker.active = {
        f"task-{index}": SimpleNamespace(
            task={"id": f"task-{index}", "runId": f"run-{index}"},
            collector=SimpleNamespace(
                session_id=f"session-{index}",
                write_latency=SimpleNamespace(snapshot=lambda item=slow if index == 0 else normal: dict(item)),
            ),
        ) for index in range(task_count)
    }
    monkeypatch.setattr("camera_logs.collection.buffering.snapshot", lambda: {
        "reservedBytes": int(4.7 * 1024 * 1024), "limitBytes": 64 * 1024 * 1024, "oldestPendingMs": 3_754.0,
    })
    monkeypatch.setattr("camera_logs.logs.write_io.snapshot", lambda: {
        "concurrency": 4, "active": 4, "waiting": task_count - 4,
        "queue": {"p99Ms": 16_000.0}, "execution": {"p99Ms": 1_300.0}, "stages": {},
    })

    snapshot = worker.write_diagnostic_snapshot()
    assert snapshot["task"]["taskId"] == "task-0"
    assert snapshot["endToEnd"]["p99Ms"] == 17_700.0
    assert snapshot["writeExecutor"]["waiting"] == task_count - 4
    assert snapshot["buffer"]["reservedBytes"] == int(4.7 * 1024 * 1024)


@pytest.mark.parametrize(("sessions", "executor", "blocked"), [
    ({"activeSessions": 100, "slowSessions": 2, "normalSessions": 98}, {"concurrency": 12, "active": 2, "waiting": 0}, False),
    ({"activeSessions": 100, "slowSessions": 4, "normalSessions": 96}, {"concurrency": 12, "active": 4, "waiting": 0}, True),
    ({"activeSessions": 3, "slowSessions": 2, "normalSessions": 1}, {"concurrency": 12, "active": 2, "waiting": 0}, True),
    ({"activeSessions": 1, "slowSessions": 1, "normalSessions": 0}, {"concurrency": 12, "active": 1, "waiting": 0}, True),
])
def test_write_admission_allows_only_strictly_few_slow_sessions(sessions, executor, blocked):
    diagnostic = {"sessions": sessions, "writeExecutor": executor,
                  "buffer": {"reservedBytes": 1, "limitBytes": 100}}
    result = write_admission({"writeLatencyMs": 17_700}, diagnostic, {"writeLatencyLimitMs": 200})
    assert result["blocked"] is blocked
