"""任务及命令模型校验，确保控制台和第三方接口遵守相同输入约束。"""
import pytest
from camera_logs.common.models import ScheduledCommand, TaskCreate
from camera_logs.resources.models import ResourceInput, ResourcePatch
from pydantic import ValidationError


def test_serial_optional_credentials_and_ssh_required():
    task = TaskCreate(name=" test ", protocol="TELNET_SERIAL", ip="::1", port=9000, resourceId="device")
    assert task.name == "test"
    assert task.initialCommands == []
    with pytest.raises(ValidationError):
        TaskCreate(name="test", protocol="SSH", ip="127.0.0.1", port=22, resourceId="device")


def test_password_spaces_are_preserved():
    task = TaskCreate(name="test", protocol="SSH", ip="127.0.0.1", port=22,
                      username="root", password="  secret  ", resourceId="device")
    assert task.password == "  secret  "


def test_task_rejects_removed_coredump_monitor_input():
    """Coredump 开关已迁移到资源，任务请求携带旧字段必须被拒绝。"""
    with pytest.raises(ValidationError, match="Coredump"):
        TaskCreate(name="test", protocol="SSH", ip="127.0.0.1", port=22, resourceId="device",
                   username="root", password="secret", enableCoredumpMonitor=True)


@pytest.mark.parametrize("model", [ResourceInput, ResourcePatch])
@pytest.mark.parametrize("switch", ["enableCoredumpMonitor", "enableResourceMonitor"])
def test_serial_resource_rejects_all_device_monitor_switches(model, switch):
    """串口服务器不能保存依赖海康设备 shell 的 Coredump 或资源采样开关。"""
    values = {"name": "串口", "kind": "SERIAL_SERVER", "ip": "192.0.2.10", switch: True}
    if model is ResourcePatch:
        values["version"] = 1
    with pytest.raises(ValidationError, match="监控仅支持海康网络设备资源"):
        model(**values)


@pytest.mark.parametrize("changes", [{"totalExecutions": 0}, {"intervalSeconds": 0}, {"command": "\n"}, {"command": "a\nb"}])
def test_invalid_schedules(changes):
    with pytest.raises(ValidationError):
        ScheduledCommand(**({"command": "date", "totalExecutions": 1, "intervalSeconds": 60} | changes))
