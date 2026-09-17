"""Worker 失败隔离测试：单路停止异常不能阻止其他设备连接关闭。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.node.worker import Worker


@pytest.mark.asyncio
async def test_database_outage_stops_all_sessions_and_keeps_only_unconfirmed_instance():
    failing = SimpleNamespace(stop=AsyncMock(side_effect=OSError("flush failed")))
    stopped = SimpleNamespace(stop=AsyncMock())
    worker = Worker(SimpleNamespace(settings=SimpleNamespace(host_proc_root=None)))
    worker.active = {"failed": failing, "stopped": stopped}

    await worker.isolate_active_sessions()

    failing.stop.assert_awaited_once()
    stopped.stop.assert_awaited_once()
    assert set(worker.active) == {"failed"}


@pytest.mark.asyncio
async def test_health_stop_uses_only_current_runtime_local_liveness(monkeypatch):
    """Worker 只将本机十秒无输出证据交给事务策略，旧归属和未知替身不触发。"""
    repo = SimpleNamespace(settings=SimpleNamespace(host_proc_root=None))
    worker = Worker(repo)
    current = {"id": "current", "runId": "run", "nodeId": "node", "generation": 1}
    stale = current | {"id": "stale", "generation": 2}
    eligible = SimpleNamespace(task=current, liveness=SimpleNamespace(
        allows_health_stop=lambda *_args, **_kwargs: True), collector=None)
    unknown = SimpleNamespace(task=current | {"id": "unknown"})
    worker.active = {"current": eligible, "stale": SimpleNamespace(task=stale,
                     liveness=SimpleNamespace(allows_health_stop=lambda *_args, **_kwargs: True), collector=None), "unknown": unknown}
    stopped = []

    async def record(_repo, task, *, liveness_check):
        assert liveness_check()
        stopped.append(task["id"])

    monkeypatch.setattr("camera_logs.resources.health_policy.stop_unresponsive_task", record)
    await worker.stop_unresponsive_health_failed_runtimes(
        {"current": current, "stale": current | {"id": "stale", "generation": 1},
         "unknown": current | {"id": "unknown"}}, 10,
    )

    assert stopped == ["current"]
