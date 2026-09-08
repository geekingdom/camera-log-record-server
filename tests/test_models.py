"""任务及命令模型校验，确保控制台和第三方接口遵守相同输入约束。"""
import pytest
from camera_logs.common.models import ScheduledCommand, TaskCreate
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


@pytest.mark.parametrize("changes", [{"totalExecutions": 0}, {"intervalSeconds": 0}, {"command": "\n"}, {"command": "a\nb"}])
def test_invalid_schedules(changes):
    with pytest.raises(ValidationError):
        ScheduledCommand(**({"command": "date", "totalExecutions": 1, "intervalSeconds": 60} | changes))
