"""判断采集连接能否承担资源级设备监控。"""

from typing import Any


def supports_resource_monitoring(task: dict[str, Any]) -> bool:
    """仅主机 SSH 与设备 Telnet 可复用连接下发资源级监控命令。

    旧 SSH 任务未保存 ``sshTarget`` 时保持与历史行为一致，按主机连接处理；
    从机 SSH 和串口 Telnet 只负责各自日志采集，不能成为资源级监控 owner。
    """
    if task.get("protocol") == "TELNET_DEVICE":
        return True
    return task.get("protocol") == "SSH" and task.get("sshTarget", "HOST") == "HOST"
