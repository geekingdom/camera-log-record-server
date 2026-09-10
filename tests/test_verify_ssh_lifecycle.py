"""SSH 实机生命周期验证脚本的受控任务选择和收尾回归。"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_ssh_lifecycle as verify


def valid_task():
    """返回允许进入实机验证的最小静态任务配置。"""
    return {
        "id": "task-a", "protocol": "SSH", "port": 22, "status": "STOPPED",
        "desiredState": "STOPPED", "nodeId": None, "ip": "192.0.2.34",
        "enableCoredumpMonitor": False, "scheduledCommands": [],
        "initialCommands": [
            {"command": "outputClose", "delaySeconds": 0.3},
            {"command": "outputOpen", "delaySeconds": 0.3},
            {"command": "setDebug -m all -l 7 -d 111", "delaySeconds": 0.3},
            {"command": "prtHardInfo", "delaySeconds": 0.3},
        ],
    }


@pytest.mark.parametrize("changes", [
    {"protocol": "TELNET_DEVICE"}, {"port": 23}, {"status": "PAUSED"},
    {"desiredState": "RUNNING"}, {"nodeId": "worker"},
    {"enableCoredumpMonitor": True}, {"scheduledCommands": [{"command": "status"}]},
    {"initialCommands": [{"command": "outputOpen", "delaySeconds": 0.3}]},
])
def test_validate_task_rejects_any_non_dedicated_lifecycle_task(changes):
    """实机脚本拒绝不满足固定契约的任务，不按名称猜测或临时修正配置。"""
    with pytest.raises(ValueError):
        verify.validate_task(valid_task() | changes)


def test_validate_task_accepts_legacy_missing_coredump_setting_but_not_missing_desired_state():
    """旧任务的 coredump 缺省值为禁用，运行意图字段则必须由正式 API 明确返回。"""
    legacy_task = valid_task()
    legacy_task.pop("enableCoredumpMonitor")
    verify.validate_task(legacy_task)
    missing_desired_state = valid_task()
    missing_desired_state.pop("desiredState")
    with pytest.raises(ValueError):
        verify.validate_task(missing_desired_state)


def test_select_tasks_requires_explicit_ids_and_rejects_another_active_endpoint_task():
    """脚本不得按名称回退选择，且同端口已有采集时必须在启动前拒绝。"""
    selected = valid_task()
    other = valid_task() | {"id": "task-b", "status": "COLLECTING", "desiredState": "RUNNING",
                            "nodeId": "worker", "ip": "192.0.2.35"}
    selected["ip"] = other["ip"]

    with pytest.raises(ValueError, match="其他活动任务"):
        verify.select_tasks([selected, other], ["task-a"])
    with pytest.raises(ValueError, match="未找到"):
        verify.select_tasks([selected], ["missing"])


def test_cleanup_stops_every_explicit_task_after_an_individual_failure():
    """一个任务 stop 请求失败时仍继续收尾其它任务，避免测试遗留采集连接。"""
    class Response:
        def __init__(self, value=None, error=None):
            self.value, self.error = value, error

        def raise_for_status(self):
            if self.error:
                raise self.error

        def json(self):
            return self.value

    class Client:
        def __init__(self):
            self.posts, self.gets = [], []

        async def post(self, path):
            self.posts.append(path)
            if path.endswith("task-a/stop"):
                return Response(error=RuntimeError("stop failed"))
            return Response({"id": "operation-b"})

        async def get(self, path):
            self.gets.append(path)
            if path.endswith("/tasks/task-b"):
                return Response(valid_task() | {"id": "task-b"})
            return Response({"status": "SUCCEEDED"})

    client = Client()
    original_connection_counts = verify.connection_counts
    verify.connection_counts = lambda _pid, tasks: {task["id"]: 0 for task in tasks}
    try:
        result = asyncio.run(verify.cleanup_tasks(client, ["task-a", "task-b"], 1234, poll_interval=0))
    finally:
        verify.connection_counts = original_connection_counts

    assert client.posts == ["/api/v1/tasks/task-a/stop", "/api/v1/tasks/task-b/stop"]
    assert client.gets == ["/api/v1/operations/operation-b", "/api/v1/tasks/task-b"]
    assert result["task-a"] == "RuntimeError" and result["task-b"] == "SUCCEEDED"


def test_load_tasks_reads_explicit_target_and_all_endpoint_pages_before_selecting():
    """目标不在首页时仍按 ID 读取，第二页活动同端点任务也必须阻止验证。"""
    class Response:
        def __init__(self, value):
            self.value = value

        def raise_for_status(self):
            pass

        def json(self):
            return self.value

    selected = valid_task()
    active = valid_task() | {"id": "task-b", "status": "COLLECTING", "desiredState": "RUNNING",
                             "nodeId": "worker"}

    class Client:
        def __init__(self):
            self.requests = []

        async def get(self, path, params=None):
            self.requests.append((path, params))
            if path == "/api/v1/tasks/task-a":
                return Response(selected)
            if params["page"] == 1:
                return Response({"items": [{"id": f"unrelated-{index}"} for index in range(100)], "total": 101})
            return Response({"items": [active], "total": 101})

    client = Client()
    with pytest.raises(ValueError, match="其他活动任务"):
        asyncio.run(verify.load_tasks_for_validation(client, ["task-a"]))
    assert client.requests == [
        ("/api/v1/tasks/task-a", None),
        ("/api/v1/tasks", {"search": "192.0.2.34", "page": 1, "pageSize": 100}),
        ("/api/v1/tasks", {"search": "192.0.2.34", "page": 2, "pageSize": 100}),
    ]


def test_record_cleanup_result_marks_a_completed_workflow_as_failed():
    """收尾任一任务失败时不能继续报告整轮生命周期验证通过。"""
    report = {"passed": True}
    verify.record_cleanup_result(report, {"task-a": "RuntimeError", "task-b": "SUCCEEDED"})
    assert report == {
        "passed": False,
        "cleanup": {"task-a": "RuntimeError", "task-b": "SUCCEEDED"},
        "error": {"type": "CleanupError", "message": "任务收尾失败: task-a"},
    }
