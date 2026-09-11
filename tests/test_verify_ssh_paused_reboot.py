"""暂停态设备重启实机验证脚本的安全前置与观察边界回归。"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_ssh_paused_reboot as verify


def task(**changes):
    """返回符合 SSH 生命周期验证器固定配置的最小任务快照。"""
    value = {
        "id": "reboot-task", "resourceId": "resource", "protocol": "SSH", "ip": "192.0.2.35", "port": 22,
        "status": "STOPPED", "desiredState": "STOPPED", "nodeId": None, "runId": "run-a", "sessionId": None,
        "generation": 1, "enableCoredumpMonitor": False, "scheduledCommands": [], "initialCommands": [
            {"command": "outputClose", "delaySeconds": 0.3},
            {"command": "outputOpen", "delaySeconds": 0.3},
            {"command": "setDebug -m all -l 7 -d 111", "delaySeconds": 0.3},
            {"command": "prtHardInfo", "delaySeconds": 0.3},
        ],
    }
    return value | changes


def test_execute_requires_explicit_reboot_confirmation_before_any_client_access():
    """遗漏危险确认参数时不能读取或写入任何正式 API。"""
    args = SimpleNamespace(reboot_confirm=False, timeout=420)
    with pytest.raises(ValueError, match="reboot-confirm"):
        asyncio.run(verify.execute(args))


def test_validate_arguments_rejects_multiple_task_ids_and_short_or_excessive_timeout():
    """脚本只允许一条受控任务，且长重启观察窗口必须覆盖65秒要求。"""
    for args in (
        SimpleNamespace(task_id=["one", "two"], timeout=420),
        SimpleNamespace(task_id=["one"], timeout=64),
        SimpleNamespace(task_id=["one"], timeout=421),
    ):
        with pytest.raises(ValueError):
            verify.validate_arguments(args)


@pytest.mark.parametrize("resource, message", [
    ({"kind": "SERIAL_SERVER", "deletedAt": None, "ip": "192.0.2.35"}, "HIKVISION_NETWORK"),
    ({"kind": "HIKVISION_NETWORK", "deletedAt": "2026-09-11T00:00:00Z", "ip": "192.0.2.35"}, "已删除"),
    ({"kind": "HIKVISION_NETWORK", "deletedAt": None, "ip": "192.0.2.36"}, "IP"),
])
def test_validate_reboot_resource_rejects_non_hikvision_deleted_or_drifted_endpoint(resource, message):
    """重启命令前必须拒绝非海康、已删除或端点不一致的资源。"""
    with pytest.raises(ValueError, match=message):
        verify.validate_reboot_resource(task(), resource)


def test_assert_paused_snapshot_rejects_auto_recovery_or_connection_residue(monkeypatch):
    """离线等待期间状态、运行身份、FD 和名额任何一个变化都必须中止验收。"""
    class Observer:
        async def count(self, _task): return 0

    current = task(status="PAUSED", desiredState="PAUSED", runId="run-a", nodeId=None)
    monkeypatch.setattr(verify, "connection_counts", lambda _pid, _tasks: {"reboot-task": 0})
    asyncio.run(verify.assert_paused_snapshot(Observer(), current, "run-a", 1234))

    for broken in (
        current | {"status": "WAITING_DEVICE"},
        current | {"desiredState": "RUNNING"},
        current | {"runId": "run-b"},
        current | {"nodeId": "worker"},
    ):
        with pytest.raises(AssertionError):
            asyncio.run(verify.assert_paused_snapshot(Observer(), broken, "run-a", 1234))


def test_assert_paused_snapshot_rejects_socket_or_slot_residue(monkeypatch):
    """暂停状态必须已主动释放采集 TCP FD 和 Mongo SSH 名额。"""
    class Observer:
        async def count(self, _task): return 1

    monkeypatch.setattr(verify, "connection_counts", lambda _pid, _tasks: {"reboot-task": 1})
    with pytest.raises(AssertionError, match="FD"):
        asyncio.run(verify.assert_paused_snapshot(Observer(), task(status="PAUSED", desiredState="PAUSED"), "run-a", 1234))


def test_pause_window_requires_65_seconds_since_pause_and_current_observed_offline():
    """健康检查较晚发现 OFFLINE 不能延长暂停验收窗口，也不能伪称实际离线时长。"""
    assert verify.pause_window_satisfied(65, 60, "OFFLINE") == (True, 5)
    assert verify.pause_window_satisfied(64.9, 0, "OFFLINE") == (False, 64.9)
    assert verify.pause_window_satisfied(65, 60, "ONLINE") == (False, 5)


def test_offline_resume_waiting_device_keeps_same_run_and_zero_connection(monkeypatch):
    """离线 resume 接受后，WAITING_DEVICE 必须保留原运行且还未重新建立 SSH。"""
    class Response:
        status_code = 202
        def raise_for_status(self): pass
        def json(self): return {"id": "resume-operation"}

    class Client:
        async def get(self, path):
            if path.endswith("/resources/resource"):
                return ResponseWith({"healthStatus": "OFFLINE"})
            return ResponseWith(task(status="WAITING_DEVICE", desiredState="RUNNING", nodeId=None))
        async def post(self, path):
            assert path.endswith("/tasks/reboot-task/resume")
            return Response()

    class ResponseWith:
        def __init__(self, value): self.value = value
        def raise_for_status(self): pass
        def json(self): return self.value

    class Observer:
        async def count(self, _task): return 0

    monkeypatch.setattr(verify, "connection_counts", lambda _pid, _tasks: {"reboot-task": 0})
    operation_id, accepted = asyncio.run(verify.request_resume_while_offline(
        Client(), "reboot-task", "resource", "run-a", 1234, Observer()))
    assert operation_id == "resume-operation" and accepted["status"] == "WAITING_DEVICE"
