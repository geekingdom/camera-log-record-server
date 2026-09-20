"""任务及命令模型校验，确保控制台和第三方接口遵守相同输入约束。"""
import pytest
from camera_logs.common.models import ScheduledCommand, TaskCreate
from camera_logs.resources.models import ResourceBatchInput, ResourceInput, ResourcePatch
from pydantic import ValidationError


def test_serial_optional_credentials_and_ssh_required():
    task = TaskCreate(name=" test ", protocol="TELNET_SERIAL", ip="::1", port=9000, resourceId="device")
    assert task.name == "test"
    assert task.initialCommands == []
    with pytest.raises(ValidationError):
        TaskCreate(name="test", protocol="SSH", ip="127.0.0.1", port=22, resourceId="device",
                   useResourceCredentials=False)


def test_password_spaces_are_preserved():
    task = TaskCreate(name="test", protocol="SSH", ip="127.0.0.1", port=22,
                      username="root", password="  secret  ", resourceId="device")
    assert task.password == "  secret  "


def test_network_task_can_explicitly_reuse_saved_resource_credentials():
    """复用已认证资源时不要求浏览器再次携带设备密码。"""
    task = TaskCreate(name="test", protocol="SSH", ip="127.0.0.1", port=22,
                      resourceId="device", useResourceCredentials=True)
    assert task.useResourceCredentials is True and task.password == ""


def test_network_task_without_resource_reuse_still_requires_credentials():
    """关闭资源凭据复用后，空凭据不能绕过连接参数校验。"""
    with pytest.raises(ValueError):
        TaskCreate(name="test", protocol="SSH", ip="127.0.0.1", port=22,
                   resourceId="device", useResourceCredentials=False)


def test_resource_input_uses_normalized_ip_when_creation_name_is_missing():
    """新建设备可省略展示名，保存前统一回退到已规范化的网络地址。"""
    resource = ResourceInput(kind="HIKVISION_NETWORK", ip="2001:0db8::8", username="admin", password="secret")
    assert resource.name == "2001:db8::8"
    assert ResourceInput(name="  ", kind="HIKVISION_NETWORK", ip="192.0.2.8", username="admin", password="secret").name == "192.0.2.8"
    with pytest.raises(ValidationError, match="名称不能为空"):
        ResourcePatch(version=1, name=" ", kind="HIKVISION_NETWORK", ip="192.0.2.8", username="admin")


def test_resource_batch_rejects_duplicate_or_non_network_addresses():
    """批量入口只接受一至256个互异的海康网络设备，避免认证后才发现请求内冲突。"""
    item = {"kind": "HIKVISION_NETWORK", "ip": "192.0.2.8", "username": "admin", "password": "secret"}
    with pytest.raises(ValidationError, match="重复"):
        ResourceBatchInput(items=[item, item])
    with pytest.raises(ValidationError, match="海康网络设备"):
        ResourceBatchInput(items=[{"name": "串口", "kind": "SERIAL_SERVER", "ip": "192.0.2.9"}])


def test_ssh_target_defaults_to_host_and_rejects_slaves_for_non_ssh_protocols():
    """从机目标只适用于 SSH，省略字段时兼容既有任务的主机采集语义。"""
    base = {"name": "test", "ip": "127.0.0.1", "port": 22, "resourceId": "device",
            "username": "root", "password": "secret"}
    assert TaskCreate(protocol="SSH", **base).sshTarget == "HOST"
    assert TaskCreate(protocol="SSH", sshTarget="SLAVE_2", **base).sshTarget == "SLAVE_2"
    with pytest.raises(ValidationError, match="SSH"):
        TaskCreate(protocol="TELNET_DEVICE", sshTarget="SLAVE_1", **base)


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
