"""可选设备监控的装配失败不应重建采集连接，后台重试须绑定原会话。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from camera_logs.collection import runtime_monitors


def monitor_runtime():
    """只创建监控装配所需状态，不建立真实协议连接或访问设备。"""
    collector = SimpleNamespace(_closed=asyncio.Event(), start_coredump_monitor=Mock())
    runtime = SimpleNamespace(
        collector=collector, stopping=False, retired=False, task={"id": "task", "resourceId": "resource"},
        repo=SimpleNamespace(settings=SimpleNamespace(nfs_server_ip="", nfs_root="/exports"),
                             db=SimpleNamespace(resources=SimpleNamespace(find_one=AsyncMock(return_value=None)))),
        coredump_target=AsyncMock(), coredump_guard=AsyncMock(), on_coredump=AsyncMock(),
    )
    return runtime, collector


async def test_source_query_failure_retries_without_closing_collector():
    """查询来源暂时失败后，后台重试成功并绑定原采集器。"""
    runtime, collector = monitor_runtime()
    runtime.coredump_target.side_effect = [RuntimeError("temporary database failure"), "10.0.0.1:/exports/device"]
    await runtime_monitors.attach_coredump_monitor(runtime, collector, retry_seconds=0)
    assert runtime.coredump_target.await_count == 2
    collector.start_coredump_monitor.assert_called_once()
    assert not collector._closed.is_set()


async def test_missing_source_can_be_discovered_later():
    """非NFS节点启动后若另一节点登记来源，现有连接也应进入共享监控。"""
    runtime, collector = monitor_runtime()
    runtime.coredump_target.side_effect = [None, "10.0.0.1:/exports/device"]
    await runtime_monitors.attach_coredump_monitor(runtime, collector, retry_seconds=0)
    assert runtime.coredump_target.await_count == 2
    collector.start_coredump_monitor.assert_called_once()


async def test_old_collector_cannot_attach_after_source_query():
    """数据库响应返回前发生会话更换，旧装配协程不得再启动监控。"""
    runtime, collector = monitor_runtime()

    async def replaced():
        runtime.collector = object()
        return "10.0.0.1:/exports/device"

    runtime.coredump_target.side_effect = replaced
    await runtime_monitors.attach_coredump_monitor(runtime, collector, retry_seconds=0)
    collector.start_coredump_monitor.assert_not_called()


async def test_supervisor_cancellation_closes_both_optional_tasks(monkeypatch):
    """NFS来源查询挂起时CPU仍启动，取消上级后两个子协程都必须结束。"""
    runtime, collector = monitor_runtime()
    starts = [asyncio.Event(), asyncio.Event()]
    exits = [asyncio.Event(), asyncio.Event()]

    async def child(index):
        starts[index].set()
        try:
            await asyncio.Future()
        finally:
            exits[index].set()

    monkeypatch.setattr(runtime_monitors, "attach_coredump_monitor", lambda *_: child(0))
    monkeypatch.setattr(runtime_monitors, "monitor_loop", lambda *_: child(1))
    supervisor = asyncio.create_task(runtime_monitors.monitor_session(runtime, collector))
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in starts)), 1)
    supervisor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await supervisor
    assert all(event.is_set() for event in exits)
