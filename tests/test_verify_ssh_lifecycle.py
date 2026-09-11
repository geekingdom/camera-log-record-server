"""SSH 实机生命周期验证脚本的受控任务选择和收尾回归。"""

import asyncio
import sys
from datetime import UTC, datetime
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


@pytest.mark.parametrize("cycles", [0, -1, False])
def test_execute_rejects_non_positive_cycles_before_settings_or_api_access(cycles):
    """空循环不能绕过暂停恢复断言，也不能触发实机前置访问。"""
    with pytest.raises(ValueError, match="cycles"):
        asyncio.run(verify.execute(type("Args", (), {"cycles": cycles})()))


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


def test_select_tasks_rejects_another_active_task_on_same_ip_with_different_ssh_port():
    """设备 SSH 名额按 IP 而非端口计，实机脚本不能遗漏另一 SSH 端口的活动任务。"""
    selected = valid_task()
    other = valid_task() | {"id": "task-b", "port": 2222, "status": "COLLECTING",
                            "desiredState": "RUNNING", "nodeId": "worker"}
    with pytest.raises(ValueError, match="其他活动任务"):
        verify.select_tasks([selected, other], ["task-a"])


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


def test_cleanup_rejects_stopped_snapshot_with_running_intent():
    """收尾不能把已排队重新启动的 STOPPED 快照误报为已完成。"""
    class Response:
        def __init__(self, value): self.value = value
        def raise_for_status(self): pass
        def json(self): return self.value

    class Client:
        async def post(self, _path): return Response({"id": "operation"})
        async def get(self, path):
            if path.endswith("/tasks/task-a"):
                return Response(valid_task() | {"desiredState": "RUNNING"})
            return Response({"status": "SUCCEEDED"})

    original_connection_counts = verify.connection_counts
    verify.connection_counts = lambda _pid, tasks: {task["id"]: 0 for task in tasks}
    try:
        result = asyncio.run(verify.cleanup_tasks(Client(), ["task-a"], 1234, poll_interval=0))
    finally:
        verify.connection_counts = original_connection_counts

    assert result == {"task-a": "RuntimeError"}


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


def test_slot_assertions_keep_current_generation_and_reject_any_stop_residue():
    """采集状态只计算精确运行，停止态必须检查同任务的所有历史代次。"""
    class Observer:
        async def count(self, _task):
            return 1

        async def count_task(self, _task):
            return 2

    assert asyncio.run(verify.assert_slot_counts(Observer(), [valid_task() | {
        "runId": "run-a", "generation": 3}], 1)) == {"task-a": 1}
    with pytest.raises(AssertionError, match="仍残留"):
        asyncio.run(verify.assert_task_slots_released(Observer(), valid_task()))


def test_idle_timeout_evidence_requires_old_session_event_after_output_close():
    """空闲模式只有读到本次旧会话事件，才允许将新会话归因于空闲超时。"""
    class Observer:
        def __init__(self, recorded): self.recorded = recorded
        async def idle_timeout_recorded(self, task, session, started):
            assert task["id"] == "task-a" and session == "session-old"
            assert started == datetime(2026, 9, 11, tzinfo=UTC)
            return self.recorded

    task = valid_task() | {"runId": "run-a"}
    stamp = datetime(2026, 9, 11, tzinfo=UTC)
    asyncio.run(verify.assert_idle_timeout_evidence(Observer(True), task, "session-old", stamp))
    with pytest.raises(AssertionError, match="IDLE_TIMEOUT"):
        asyncio.run(verify.assert_idle_timeout_evidence(Observer(False), task, "session-old", stamp))


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "UNKNOWN"])
def test_idle_reconnect_rejects_non_sent_output_close_without_waiting_for_device(status):
    """空闲重连只能以明确 SENT 的 outputClose 为起点，结果未知不能当作成功。"""
    class Response:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    class Client:
        async def post(self, *_args, **_kwargs): return Response({"id": "command"})
        async def get(self, *_args, **_kwargs): return Response({"status": status})

    task = valid_task() | {"status": "COLLECTING", "desiredState": "RUNNING",
                           "runId": "run-a", "sessionId": "session-a", "generation": 1,
                           "nodeId": "worker"}
    with pytest.raises(RuntimeError, match="outputClose"):
        asyncio.run(verify.verify_idle_reconnect(Client(), task, 0, object(), timeout=0))
