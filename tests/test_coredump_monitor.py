"""挂载监控不增加连接，失败隔离且不吞掉会话取消。"""

import asyncio

import pytest
from camera_logs.collection.collector import Collector
from camera_logs.collection.coredump_cleanup import CoredumpMountCleanup
from camera_logs.collection.coredump_monitor import monitor_mount, mount_target


def test_target_uses_device_ip_and_rejects_shell_syntax():
    """目录按设备 IP 隔离，不能注入额外命令。"""
    directory, target = mount_target("10.0.0.1", "/srv/core", "10.0.0.34")
    assert str(directory) == "/srv/core/10.0.0.34"
    assert target == "10.0.0.1:/srv/core/10.0.0.34"
    for root in ("relative", "/srv/../core", "/srv/core;id", "/srv/$(id)"):
        with pytest.raises(ValueError):
            mount_target("10.0.0.1", root, "10.0.0.34")


async def test_monitor_mounts_once_then_checks_and_cancels():
    """成功挂载后只查状态，不重复发送挂载配置。"""
    commands, events = [], []

    async def send(command, **kwargs):
        commands.append((command, kwargs))

    async def report(status, error):
        events.append((status, error))
        if len(events) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await monitor_mount(send, report, "10.0.0.1:/srv/core/10.0.0.34", interval=0)
    assert [command.split()[0] for command, _ in commands] == ["debug", "gdbcfg", "mount", "mount"]
    assert events == [("MOUNTED", None), ("MOUNTED", None)]


async def test_monitor_recovers_missing_mount_without_repeating_debug():
    """挂载丢失时重新配置，保持连接和日志采集。"""
    commands, events = [], []

    async def send(command, **kwargs):
        commands.append(command.split()[0])
        if commands == ["debug", "gdbcfg", "mount", "mount"]:
            raise TimeoutError

    async def report(status, error):
        events.append(status)
        if len(events) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await monitor_mount(send, report, "10.0.0.1:/srv/core/10.0.0.34", interval=0)
    assert commands == ["debug", "gdbcfg", "mount", "mount", "gdbcfg", "mount"]


async def test_failed_debug_is_not_retried_in_same_session():
    """解密失败仅记事件，不形成自动口令重试循环。"""
    commands, events = [], []

    async def send(command, **kwargs):
        commands.append(command)
        raise RuntimeError("sensitive error")

    async def report(status, error):
        events.append((status, error))

    await monitor_mount(send, report, "10.0.0.1:/srv/core/10.0.0.34", interval=0)
    assert commands == ["debug"]
    assert events == [("FAILED", "RuntimeError")]


async def test_guard_stops_monitor_before_any_debug_or_mount_command():
    """任务归属或资源租约失效时，后台监控不得在共享会话中写入任何设备命令。"""
    commands = []

    async def send(command, **_kwargs):
        commands.append(command)

    async def report(*_args):
        raise AssertionError("失效监控不应写状态")

    async def guard():
        return False

    await monitor_mount(send, report, "10.0.0.1:/srv/core/10.0.0.34", guard=guard)
    assert commands == []


async def test_sender_guard_blocks_command_that_lost_ownership_while_queued():
    """轮询领取成功后若在队列中失属，真正发送阶段必须拒绝该设备命令。"""
    calls = []

    async def send(command, *, session_guard=None, **_kwargs):
        calls.append(command)
        await session_guard()

    async def report(*_args):
        return None

    guards = iter([True, False])

    async def guard():
        return next(guards)

    await monitor_mount(send, report, "10.0.0.1:/srv/core/10.0.0.34", interval=0, guard=guard)
    assert calls == ["debug"]


async def test_waiting_competitor_replaces_cleanup_target_before_first_mount_attempt():
    """竞争失败后必须收尾赢家来源，不能保留本节点尚未挂载的旧 target。"""
    loser = "10.0.0.2:/srv/core/10.0.0.34"
    winner = "10.0.0.1:/srv/core/10.0.0.34"
    current = loser
    cleanup = CoredumpMountCleanup(lambda: True)
    cleanup.configure(loser)
    commands = []
    claims = 0

    async def guard():
        nonlocal claims, current
        claims += 1
        if claims == 1:
            current = winner
            return None
        return True

    async def send(command, **_kwargs):
        commands.append(command)
        if command == "mount":
            raise asyncio.CancelledError

    async def report(*_args):
        return None

    with pytest.raises(asyncio.CancelledError):
        await monitor_mount(send, report, lambda: current, interval=0, guard=guard, cleanup=cleanup)

    assert cleanup._target == winner
    assert cleanup._mount_attempted
    assert any(winner in command for command in commands)
    assert all(loser not in command for command in commands)


async def test_collector_monitor_uses_existing_sender_lifecycle(tmp_path, monkeypatch):
    """监控作为采集器受管任务启动，停止采集器会取消它，不会建立第二条设备连接。"""
    started = asyncio.Event()

    async def fake_monitor(collector, _server, _root, _report, *, guard=None, cleanup=None):
        assert collector._connection is connection and guard is not None
        assert cleanup is not None
        started.set()
        await asyncio.Future()

    connection = object()
    collector = Collector({"id": "task", "runId": "run", "storageIdentity": "device"}, tmp_path,
                          connection_factory=lambda _: connection)
    collector._connection = connection
    collector._initializing = False
    monkeypatch.setattr("camera_logs.collection.coredump_monitor.run_monitor", fake_monitor)
    collector.start_coredump_monitor("10.0.0.1", "/srv/core", lambda *_: None, guard=lambda: True)
    await started.wait()
    task = collector._coredump_monitor
    assert task is not None
    task.cancel()
    assert (await asyncio.gather(task, return_exceptions=True))[0] is not None
