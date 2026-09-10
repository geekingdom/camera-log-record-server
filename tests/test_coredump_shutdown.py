"""Coredump NFS 关闭收尾回归：EOF、取消和并发停止必须回收同一采集连接。"""

from __future__ import annotations

import asyncio
import re

from camera_logs.collection import coredump_cleanup
from camera_logs.collection.collector import Collector
from camera_logs.collection.coredump_cleanup import CoredumpMountCleanup

TARGET = "10.0.0.1:/srv/coredump/10.0.0.34"


class ShutdownConnection:
    """用真实 read 队列模拟设备响应，close 通过 EOF 唤醒接收器。"""

    def __init__(self, *, reply_to_marker: bool) -> None:
        self.reply_to_marker = reply_to_marker
        self.received: asyncio.Queue[bytes] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.close_calls = 0

    async def read(self, _size=65536):
        return await self.received.get()

    async def write(self, data):
        self.sent.append(data)
        marker = re.search(rb"(coredump-unmount-[0-9a-f]+)\s+([0-9a-f]+)", data)
        if self.reply_to_marker and marker:
            await self.received.put(marker.group(1) + marker.group(2) + b"\n")

    async def close(self):
        self.close_calls += 1
        await self.received.put(b"")


async def _collector_with_attempted_mount(tmp_path, connection):
    """创建已尝试挂载的采集器，保留真实 reader、sender 与 socket close 路径。"""
    reports = []
    collector = Collector(
        {"id": "shutdown", "runId": "run", "storageIdentity": "test", "ip": "10.0.0.34"},
        tmp_path,
        connection_factory=lambda _task: connection,
    )
    await collector.start()
    cleanup = CoredumpMountCleanup(lambda: True)
    cleanup.configure(TARGET)
    cleanup.mark_mount_attempted()
    collector._coredump_cleanup = cleanup
    collector._coredump_report = lambda status, error: reports.append((status, error))
    return collector, reports


async def test_coredump_eof_records_unconfirmed_cleanup_and_closes_connection(tmp_path):
    """接收器 EOF 后不伪造卸载成功，仍由 reader finally 实际关闭连接并收束任务。"""
    connection = ShutdownConnection(reply_to_marker=False)
    collector, reports = await _collector_with_attempted_mount(tmp_path, connection)

    await connection.received.put(b"")
    await asyncio.wait_for(collector.wait_closed(), 1)
    await asyncio.gather(collector._reader, collector._sender, return_exceptions=True)

    assert reports == [("UNMOUNT_FAILED", "ConnectionError")]
    assert not any(b"umount" in command for command in connection.sent)
    assert connection.close_calls == 1


async def test_coredump_stop_cancels_monitor_then_confirms_unmount_before_close(tmp_path):
    """停止取消后台监控后仍在当前连接等待卸载 marker，再关闭 socket。"""
    async def monitor_loop():
        await asyncio.Future()

    connection = ShutdownConnection(reply_to_marker=True)
    collector, reports = await _collector_with_attempted_mount(tmp_path, connection)
    monitor = asyncio.create_task(monitor_loop())
    collector._coredump_monitor = monitor

    await collector.stop()

    assert monitor.cancelled()
    assert reports == [("UNMOUNTED", None)]
    assert sum(b"umount -l " in command for command in connection.sent) == 1
    assert connection.close_calls == 1
    assert collector._closed.is_set()


async def test_coredump_concurrent_stop_runs_one_cleanup_and_one_close(tmp_path):
    """两个停止调用竞争时锁与 finished 标志只允许一次卸载和一次连接释放。"""
    connection = ShutdownConnection(reply_to_marker=True)
    collector, reports = await _collector_with_attempted_mount(tmp_path, connection)

    await asyncio.gather(collector.stop(), collector.stop())

    assert reports == [("UNMOUNTED", None)]
    assert sum(b"umount -l " in command for command in connection.sent) == 1
    assert connection.close_calls == 1
    assert collector._closed.is_set()


async def test_cancelled_collector_stop_records_failed_unmount_and_closes_connection(tmp_path):
    """取消 stop 本身不能跳过 finally；未确认卸载必须记录取消且实际关闭连接。"""
    connection = ShutdownConnection(reply_to_marker=False)
    collector, reports = await _collector_with_attempted_mount(tmp_path, connection)
    stop_task = asyncio.create_task(collector.stop())
    while not connection.sent:
        await asyncio.sleep(0)
    stop_task.cancel()

    result = (await asyncio.gather(stop_task, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    assert reports == [("UNMOUNT_FAILED", "CancelledError")]
    assert connection.close_calls == 1
    assert collector._closed.is_set()


async def test_eof_while_waiting_for_unmount_marker_finishes_within_cleanup_timeout(tmp_path, monkeypatch):
    """卸载 marker 等待中收到 EOF 时，超时后收尾而不永久等待已断开的接收器。"""
    monkeypatch.setattr(coredump_cleanup, "CLEANUP_TIMEOUT_SECONDS", .05)
    connection = ShutdownConnection(reply_to_marker=False)
    collector, reports = await _collector_with_attempted_mount(tmp_path, connection)
    stop_task = asyncio.create_task(collector.stop())
    while not connection.sent:
        await asyncio.sleep(0)
    await connection.received.put(b"")

    await asyncio.wait_for(stop_task, 1)
    assert reports == [("UNMOUNT_FAILED", "TimeoutError")]
    assert connection.close_calls == 1
    assert collector._closed.is_set()


async def test_new_collector_in_same_run_restarts_coredump_monitor_after_cleanup(tmp_path):
    """同一运行重连产生新会话，旧 cleanup 已完成也不能抑制新会话重新挂载。"""
    task = {"id": "resume", "runId": "same-run", "storageIdentity": "test", "ip": "10.0.0.34"}
    old_collector = Collector(task, tmp_path, connection_factory=lambda _task: ShutdownConnection(reply_to_marker=True))
    old_cleanup = CoredumpMountCleanup(lambda: True)
    old_cleanup.configure(TARGET)
    old_cleanup.mark_mount_attempted()
    await old_cleanup.unavailable(lambda *_: None)
    assert old_cleanup._finished

    commands, mounted = [], asyncio.Event()
    new_collector = Collector(task, tmp_path, connection_factory=lambda _task: ShutdownConnection(reply_to_marker=True))
    new_collector._initializing = False

    async def enqueue(command, _newline, **_kwargs):
        commands.append(command)
        if command == "mount":
            mounted.set()
            raise asyncio.CancelledError

    async def guard():
        return True

    async def report(*_args):
        return None

    new_collector._enqueue = enqueue
    new_collector.start_coredump_monitor("10.0.0.1", str(tmp_path), report, guard=guard,
                                        cleanup_guard=guard)
    await asyncio.wait_for(mounted.wait(), 1)
    await asyncio.gather(new_collector._coredump_monitor, return_exceptions=True)

    assert new_collector.session_id != old_collector.session_id
    assert new_collector._coredump_cleanup is not old_cleanup
    assert any(command.startswith("gdbcfg --password=hiklinux --nfsmount=10.0.0.1:") for command in commands)
