"""串口采集在健康停机入口保持独立，网络协议仍遵循原有停机策略。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.node.worker import Worker
from camera_logs.resources.health_policy import stop_unresponsive_task


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["TELNET_SERIAL", "SSH", "TELNET_DEVICE"])
async def test_worker_health_stop_explicitly_excludes_serial(monkeypatch, protocol):
    """即使接收状态判据允许停止，Worker 也不能把串口交给设备认证停机链。"""
    worker = Worker(SimpleNamespace(settings=SimpleNamespace(host_proc_root=None)))
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1,
            "protocol": protocol, "desiredState": "RUNNING", "status": "COLLECTING"}
    runtime = SimpleNamespace(task=task.copy(), collector=None, liveness=SimpleNamespace(
        allows_health_stop=lambda *_args, **_kwargs: True))
    worker.active = {"task": runtime}
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr("camera_logs.resources.health_policy.stop_unresponsive_task", stop)

    await worker.stop_unresponsive_health_failed_runtimes({"task": task}, 20)

    if protocol == "TELNET_SERIAL":
        stop.assert_not_awaited()
        assert task == runtime.task
        assert worker.active["task"] is runtime
    else:
        stop.assert_awaited_once()
        assert task["desiredState"] == "STOPPED"


@pytest.mark.asyncio
async def test_serial_health_stop_does_not_open_database_transaction(monkeypatch):
    """串口协议在事务和数据库之前返回，认证存储不可用也不会影响该路径。"""
    transaction = AsyncMock(side_effect=AssertionError("串口不应进入认证停机事务"))
    monkeypatch.setattr("camera_logs.resources.health_policy.audited_mutations.mutation_transaction", transaction)
    task = {"id": "serial", "protocol": "TELNET_SERIAL", "resourceId": "camera", "desiredState": "RUNNING"}
    assert not await stop_unresponsive_task(SimpleNamespace(), task, liveness_check=lambda: True)
    transaction.assert_not_awaited()
    assert task["desiredState"] == "RUNNING"
