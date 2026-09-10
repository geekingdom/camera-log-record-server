"""Coredump NFS 挂载清理的 shell 合同与会话内控制器回归。"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess

import pytest
from camera_logs.collection.collector import Collector
from camera_logs.collection.coredump_cleanup import CoredumpMountCleanup, unmount_command

TARGET = "10.0.0.1:/srv/coredump/10.0.0.34"
OTHER_SOURCE = "10.0.0.2:/srv/coredump/10.0.0.35"


def run_shell(command: str, mounts, behavior: str) -> subprocess.CompletedProcess[str]:
    """以 shell 函数替代 umount，仅验证生成命令的真实 sh 行为，不触碰主机挂载。"""
    script = """
umount() {
    [ "$#" -eq 2 ] && [ "$1" = "-l" ] && [ "$2" = "$TARGET" ] || return 97
    case "$BEHAVIOR" in
        remove)
            temporary="$MOUNTS.next"
            : > "$temporary"
            while IFS= read -r line; do
                case "$line" in "$2 "*) ;; *) printf '%s\\n' "$line" >> "$temporary" ;; esac
            done < "$MOUNTS"
            mv "$temporary" "$MOUNTS"
            ;;
        keep) return 0 ;;
        fail) return 1 ;;
    esac
}
""" + command
    return subprocess.run(["/bin/sh", "-c", script], check=False, text=True, capture_output=True,
                          env=os.environ | {"MOUNTS": str(mounts), "BEHAVIOR": behavior, "TARGET": TARGET})


@pytest.mark.parametrize(("initial", "behavior", "expected_marker"), [
    (TARGET + " /mnt nfs rw 0 0\n" + OTHER_SOURCE + " /other nfs rw 0 0\n", "remove", True),
    (TARGET + " /mnt nfs rw 0 0\n", "keep", False),
    (OTHER_SOURCE + " /other nfs rw 0 0\n", "fail", True),
    (TARGET + " /mnt nfs rw 0 0\n", "fail", False),
])
def test_unmount_command_requires_success_and_exact_source_absence(tmp_path, initial, behavior, expected_marker):
    """成功卸载且目标第一列消失才打印 marker；其它挂载和回显不能伪造成功。"""
    mounts = tmp_path / "mounts"
    mounts.write_text(initial)
    marker = "cleanup-marker-a-cleanup-marker-b"
    command = unmount_command(TARGET, marker, mounts_path=mounts)

    result = run_shell(command, mounts, behavior)

    # shell 尾部保持成功退出不代表设备卸载成功，唯一成功证据是拆分 marker。
    assert (marker in result.stdout) is expected_marker
    assert marker not in command
    assert "umount -l " in command
    assert "umount -a" not in command and "umount -f" not in command
    if expected_marker:
        assert OTHER_SOURCE in mounts.read_text() and TARGET not in mounts.read_text()


def test_unmount_command_missing_mounts_never_emits_success_marker(tmp_path):
    """设备无法读取挂载表时不能把未知状态写成已卸载。"""
    marker = "cleanup-marker-a-cleanup-marker-b"
    command = unmount_command(TARGET, marker, mounts_path=tmp_path / "missing-mounts")

    result = run_shell(command, tmp_path / "missing-mounts", "remove")

    assert marker not in result.stdout


async def test_cleanup_uses_one_internal_send_after_guard_and_reports_success():
    """控制器只清理本会话曾尝试的目标，并显式标记允许关闭阶段发送。"""
    cleanup = CoredumpMountCleanup(lambda: True)
    cleanup.configure(TARGET)
    cleanup.mark_mount_attempted()
    calls, reports = [], []

    async def send(command, **kwargs):
        calls.append((command, kwargs))

    async def report(status, error):
        reports.append((status, error))

    await cleanup.run(send, report)
    await cleanup.run(send, report)

    assert len(calls) == 1
    assert calls[0][1]["allow_during_shutdown"] is True
    assert calls[0][1]["prompt"] not in calls[0][0]
    assert reports == [("UNMOUNTED", None)]


async def test_cleanup_guard_or_send_failure_never_claims_unmounted():
    """失属、guard 异常和命令超时都必须跳过或失败，不得伪造卸载完成。"""
    async def rejected():
        return False

    async def assert_guard_result(guard, expected_status, expected_error):
        cleanup = CoredumpMountCleanup(guard)
        cleanup.configure(TARGET)
        cleanup.mark_mount_attempted()
        sent, reports = [], []

        async def send(*args, **kwargs):
            sent.append((args, kwargs))

        async def report(status, error):
            reports.append((status, error))

        await cleanup.run(send, report)
        assert sent == []
        assert reports == [(expected_status, expected_error)]

    await assert_guard_result(rejected, "UNMOUNT_SKIPPED", None)
    await assert_guard_result(
        lambda: (_ for _ in ()).throw(RuntimeError("guard failed")),
        "UNMOUNT_FAILED",
        "RuntimeError",
    )

    cleanup = CoredumpMountCleanup(lambda: True)
    cleanup.configure(TARGET)
    cleanup.mark_mount_attempted()
    reports = []

    async def timeout(*_args, **_kwargs):
        raise TimeoutError

    await cleanup.run(timeout, lambda status, error: reports.append((status, error)))
    assert reports == [("UNMOUNT_FAILED", "TimeoutError")]


async def test_cleanup_rechecks_guard_before_transport_write():
    """排队期间失属时，控制器必须跳过而不是让 sender 写入卸载命令。"""
    checks, reports = [], []

    async def guard():
        checks.append(None)
        return len(checks) == 1

    async def send(*_args, **kwargs):
        await kwargs["session_guard"]()
        await kwargs["session_guard"]()

    cleanup = CoredumpMountCleanup(guard)
    cleanup.configure(TARGET)
    cleanup.mark_mount_attempted()
    await cleanup.run(send, lambda status, error: reports.append((status, error)))

    assert len(checks) == 2
    assert reports == [("UNMOUNT_SKIPPED", None)]


class CleanupConnection:
    """用接收队列模拟同一 SSH 会话对拆分 marker 的响应。"""

    def __init__(self):
        self.sent = []
        self.received = asyncio.Queue()
        self.closed = False

    async def read(self, _=65536):
        return await self.received.get()

    async def write(self, data):
        self.sent.append(data)
        match = re.search(rb"(coredump-unmount-[0-9a-f]+)\s+([0-9a-f]+)", data)
        if match:
            await self.received.put(match.group(1) + match.group(2) + b"\n")

    async def close(self):
        self.closed = True
        await self.received.put(b"")


async def test_collector_stop_uses_current_connection_and_waits_for_unmount_marker(tmp_path, monkeypatch):
    """正常停止保持接收器直到同一连接确认卸载，随后才释放该连接。"""
    connection, reports = CleanupConnection(), []

    async def monitor(_collector, _server, _root, _report, *, guard=None, cleanup=None):
        cleanup.configure(TARGET)
        cleanup.mark_mount_attempted()
        await asyncio.Future()

    monkeypatch.setattr("camera_logs.collection.coredump_monitor.run_monitor", monitor)
    collector = Collector(
        {"id": "cleanup", "runId": "run", "storageIdentity": "test", "ip": "10.0.0.34"},
        tmp_path, connection_factory=lambda _: connection,
    )
    await collector.start()
    collector.start_coredump_monitor("10.0.0.1", "/srv/coredump", lambda *event: reports.append(event),
                                    cleanup_guard=lambda: True)
    await asyncio.sleep(0)
    await collector.stop()

    assert any(b"umount -l " in command for command in connection.sent)
    assert reports == [("UNMOUNTED", None)]
    assert connection.closed
