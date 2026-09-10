"""SSH 重启恢复实机验证器的控制操作快照竞态回归。"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_ssh_reboot_recovery as verify


class Response:
    """提供验证脚本所需的最小 HTTP 响应接口。"""
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


def test_start_waits_for_fresh_collecting_snapshot_after_operation_succeeds(monkeypatch):
    """操作已成功而前一任务快照仍 CONNECTING 时，必须继续读到 COLLECTING。"""
    connecting = {"id": "task", "status": "CONNECTING", "desiredState": "RUNNING", "ip": "192.0.2.1", "port": 22}
    collecting = connecting | {"status": "COLLECTING", "runId": "run", "sessionId": "session"}

    class Client:
        def __init__(self):
            self.tasks = [connecting, collecting]

        async def post(self, path):
            assert path == "/api/v1/tasks/task/start"
            return Response({"id": "operation"})

        async def get(self, path):
            if path == "/api/v1/tasks/task":
                return Response(self.tasks.pop(0))
            assert path == "/api/v1/operations/operation"
            return Response({"status": "SUCCEEDED"})

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(verify, "connection_counts", lambda _pid, tasks: {item["id"]: 1 for item in tasks})
    monkeypatch.setattr(verify.asyncio, "sleep", no_sleep)
    result = asyncio.run(verify.start_and_wait_collecting(Client(), "task", 1234))

    assert result == collecting


def test_default_preflight_rejects_same_resource_pending_recovery(monkeypatch):
    """默认模式必须在发送任何 reboot 前拒绝同资源的自动恢复竞争者。"""
    target = {
        "id": "target", "resourceId": "resource", "protocol": "SSH", "port": 22, "status": "STOPPED",
        "desiredState": "STOPPED", "nodeId": None, "ip": "192.0.2.1", "enableCoredumpMonitor": False,
        "scheduledCommands": [], "initialCommands": [
            {"command": "outputClose", "delaySeconds": 0.3}, {"command": "outputOpen", "delaySeconds": 0.3},
            {"command": "setDebug -m all -l 7 -d 111", "delaySeconds": 0.3}, {"command": "prtHardInfo", "delaySeconds": 0.3},
        ],
    }
    pending = target | {"id": "other", "resourceHealthRecovery": {"resourceId": "resource"}}

    async def task(_client, identifier):
        assert identifier == "target"
        return target

    async def tasks(_client, params):
        return [target, pending] if "resourceId" in params else [target]

    monkeypatch.setattr(verify, "get_task", task)
    monkeypatch.setattr(verify, "list_tasks", tasks)
    with pytest.raises(ValueError, match="待恢复"):
        asyncio.run(verify.load_reboot_task(object(), "target", False))


def test_shared_endpoint_allows_socket_total_explained_by_visible_active_tasks(monkeypatch):
    """共享模式允许两个可见活动任务对应两条同端点 socket，而不主张单任务独占。"""
    target = {"id": "target", "ip": "192.0.2.1", "port": 22, "status": "COLLECTING", "nodeId": "node"}
    other = target | {"id": "other"}

    async def tasks(_client, _params):
        return [target, other]

    monkeypatch.setattr(verify, "list_tasks", tasks)
    monkeypatch.setattr(verify, "connection_counts", lambda _pid, _tasks: {"target": 2})
    detail = asyncio.run(verify.check_endpoint_sockets(object(), 1234, target, True))

    assert detail == {"socketTotal": 2, "allowedActiveTasks": 2, "activeTaskIds": ["target", "other"]}
