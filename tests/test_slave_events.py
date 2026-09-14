"""从机 SSH 引导排障事件只保存固定关联字段且不能影响采集控制流。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection import slave_events
from camera_logs.collection.slave_events import record_slave_bootstrap_event


def _task():
    """构造包含敏感运行字段的从机任务，验证事件写入器不会复制它们。"""
    return {
        "id": "slave-task", "runId": "run-1", "generation": 4, "resourceId": "resource-1",
        "sshTarget": "SLAVE_2", "nodeId": "node-a", "password": "device-password",
        "_bootstrapToken": "bootstrap-token",
    }


@pytest.mark.asyncio
async def test_slave_bootstrap_event_persists_only_fixed_diagnostic_fields():
    """持久事件可关联从机、主机和端口，且绝不携带口令、令牌或设备正文。"""
    events = SimpleNamespace(insert_one=AsyncMock())
    repo = SimpleNamespace(db=SimpleNamespace(events=events))

    await record_slave_bootstrap_event(
        repo, _task(), phase="HOST_BORROWED", outcome="SUCCEEDED", level="INFO",
        message="已通过主机采集连接提交扩展SSH引导", port=18080,
        host_task_id="host-task", host_node_id="node-b",
    )

    document = events.insert_one.call_args.args[0]
    assert document["type"] == "SLAVE_SSH_BOOTSTRAP"
    assert document["taskId"] == "slave-task"
    assert document["runId"] == "run-1"
    assert document["generation"] == 4
    assert document["resourceId"] == "resource-1"
    assert document["sshTarget"] == "SLAVE_2"
    assert document["nodeId"] == "node-a"
    assert document["port"] == 18080
    assert document["hostTaskId"] == "host-task"
    assert document["hostNodeId"] == "node-b"
    assert document["phase"] == "HOST_BORROWED"
    assert document["outcome"] == "SUCCEEDED"
    assert document["level"] == "INFO"
    assert document["message"] == "已通过主机采集连接提交扩展SSH引导"
    assert set(document) == {
        "type", "createdAt", "taskId", "runId", "generation", "resourceId", "sshTarget", "nodeId", "port",
        "hostTaskId", "hostNodeId", "phase", "outcome", "level", "message",
    }
    assert "device-password" not in str(document)
    assert "bootstrap-token" not in str(document)


@pytest.mark.asyncio
async def test_slave_bootstrap_event_write_failure_does_not_change_control_flow(caplog):
    """排障集合不可用时只输出固定安全日志，调用方仍能继续引导或清理。"""
    events = SimpleNamespace(insert_one=AsyncMock(side_effect=OSError("database private detail")))
    repo = SimpleNamespace(db=SimpleNamespace(events=events))

    await record_slave_bootstrap_event(
        repo, _task(), phase="SERVICE_NOT_READY", outcome="FAILED", level="WARNING",
        message="从机扩展SSH服务未就绪", port=18080,
    )

    assert "database private detail" not in caplog.text
    assert "从机SSH引导事件写入失败" in caplog.text


@pytest.mark.asyncio
async def test_slave_bootstrap_event_timeout_cancels_slow_write_and_returns(monkeypatch):
    """慢事件写入达到上限后取消自身写入，不能延误从机建连或清理。"""
    cancelled = False

    async def slow_write(_document):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    events = SimpleNamespace(insert_one=slow_write)
    repo = SimpleNamespace(db=SimpleNamespace(events=events))
    monkeypatch.setattr(slave_events, "_EVENT_WRITE_TIMEOUT_SECONDS", .01)

    await record_slave_bootstrap_event(
        repo, _task(), phase="SERVICE_READY", outcome="SUCCEEDED", level="INFO",
        message="从机扩展SSH服务已就绪", port=18080,
    )

    assert cancelled


@pytest.mark.asyncio
async def test_slave_bootstrap_event_propagates_caller_cancellation():
    """调用方取消时写入器不得将取消伪装成已记录或普通写入失败。"""
    started = asyncio.Event()

    async def slow_write(_document):
        started.set()
        await asyncio.Event().wait()

    repo = SimpleNamespace(db=SimpleNamespace(events=SimpleNamespace(insert_one=slow_write)))
    task = asyncio.create_task(record_slave_bootstrap_event(
        repo, _task(), phase="REMOTE_RESULT_UNKNOWN", outcome="UNKNOWN", level="WARNING",
        message="跨节点主机引导请求已取消，保留资源引导租约", port=18080,
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
