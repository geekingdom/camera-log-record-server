"""定义设备资源接口的输入模型，并在请求边界校验设备类型与凭据。"""

from ipaddress import ip_address
from typing import Literal

from pydantic import Field, field_validator, model_validator

from camera_logs.common.models import Model


class ResourceInput(Model):
    """描述一个可供任务引用的网络设备或串口服务器资源。"""

    name: str | None = Field(default=None, max_length=128, description="可省略；空白时保存为规范 IP")
    kind: Literal["HIKVISION_NETWORK", "SERIAL_SERVER"] = Field(description='设备类型：HIKVISION_NETWORK 为海康网络设备，SERIAL_SERVER 为串口服务器。')
    ip: str = Field(description='设备资源、串口服务器或采集连接目标的 IPv4/IPv6 地址。')
    username: str = Field(default="", max_length=256, description='登录设备或平台账户使用的用户名。')
    password: str = Field(default="", max_length=4096, description='海康网络资源认证用登录密码；新建网络资源必须提供，串口服务器不可填写。')
    authType: Literal["DIGEST", "BASIC"] = Field(default="DIGEST", description='访问海康 ISAPI 设备信息接口时使用的 HTTP 认证方式。')
    enableCoredumpMonitor: bool = Field(default=False, description='海康设备资源是否启用 Coredump NFS 监控；仅有正在采集的 SSH 或 Telnet 设备任务时执行。')
    # 资源级 CPU/内存监控开关由采集会话按资源归属和租约动态执行。
    enableResourceMonitor: bool = Field(default=False, description='海康设备资源是否启用每分钟 CPU 与内存采样；复用正在采集的设备连接。')

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        """名称去除首尾空白，避免视觉相同的空白资源名进入数据库。"""
        return value.strip() if value is not None else None

    @field_validator("ip")
    @classmethod
    def valid_ip(cls, value):
        """资源地址必须为规范的 IPv4 或 IPv6 文字地址。"""
        return str(ip_address(value))

    @model_validator(mode="after")
    def validate_kind_credentials(self):
        """网络设备需要完整凭据，串口服务器只允许名称和地址配置。"""
        # 仅新建资源允许省略或留白名称，使用已规范化 IP 作为稳定的默认展示名。
        if not self.name:
            self.name = self.ip
        if self.kind == "HIKVISION_NETWORK" and (not self.username.strip() or not self.password):
            raise ValueError("海康网络设备必须填写用户名和密码")
        if self.kind == "SERIAL_SERVER" and (self.username or self.password):
            raise ValueError("串口服务器不支持用户名或密码")
        if self.kind != "HIKVISION_NETWORK" and (self.enableCoredumpMonitor or self.enableResourceMonitor):
            raise ValueError("设备监控仅支持海康网络设备资源")
        return self


class ResourceBatchInput(Model):
    """限制批量新建为互异的海康网络设备，避免请求内地址冲突晚于认证才暴露。"""

    items: list[ResourceInput] = Field(
        min_length=1, max_length=256, description="1 至 256 个仅限 HIKVISION_NETWORK 的新建资源",
    )

    @model_validator(mode="after")
    def validate_items(self):
        """批量探测只适用于具备 HTTP 认证合同的海康网络资源。"""
        addresses = [item.ip for item in self.items]
        if any(item.kind != "HIKVISION_NETWORK" for item in self.items):
            raise ValueError("批量创建仅支持海康网络设备")
        if len(addresses) != len(set(addresses)):
            raise ValueError("批量创建不能包含重复 IP")
        return self


class ResourcePatch(Model):
    """更新资源展示名称或 HTTP 凭据时提交的完整稳定身份与乐观锁版本。"""

    version: int = Field(ge=1, description='当前配置版本号，用于乐观锁并发修改校验。')
    name: str = Field(min_length=1, max_length=128, description='资源、任务、模板、节点或服务账号的用户可识别名称。')
    kind: Literal["HIKVISION_NETWORK", "SERIAL_SERVER"] = Field(description='设备类型：HIKVISION_NETWORK 为海康网络设备，SERIAL_SERVER 为串口服务器。')
    ip: str = Field(description='设备资源、串口服务器或采集连接目标的 IPv4/IPv6 地址。')
    username: str = Field(default="", max_length=256, description='登录设备或平台账户使用的用户名。')
    password: str = Field(default="", max_length=4096, description='海康网络资源的新登录密码；空字符串保留已存密码。串口服务器不可填写凭据。')
    authType: Literal["DIGEST", "BASIC"] = Field(default="DIGEST", description='访问海康 ISAPI 设备信息接口时使用的 HTTP 认证方式。')
    enableCoredumpMonitor: bool = Field(default=False, description='海康设备资源是否启用 Coredump NFS 监控；仅有正在采集的 SSH 或 Telnet 设备任务时执行。')
    # 与创建模型一致地完整提交，采集会话据此动态调整资源监控。
    enableResourceMonitor: bool = Field(default=False, description='海康设备资源是否启用每分钟 CPU 与内存采样；复用正在采集的设备连接。')

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        """名称去除首尾空白，避免编辑产生仅由空白构成的展示名称。"""
        if not value.strip():
            raise ValueError("名称不能为空")
        return value.strip()

    @field_validator("ip")
    @classmethod
    def valid_ip(cls, value):
        """更新请求仍必须携带规范 IP，服务端据此拒绝物理连接目标变更。"""
        return str(ip_address(value))

    @model_validator(mode="after")
    def validate_kind_credentials(self):
        """串口服务器不接受 HTTP 凭据，网络资源允许空密码表达保留旧密码。"""
        if self.kind == "HIKVISION_NETWORK" and not self.username.strip():
            raise ValueError("海康网络设备必须填写用户名")
        if self.kind == "SERIAL_SERVER" and (self.username or self.password):
            raise ValueError("串口服务器不支持用户名或密码")
        if self.kind != "HIKVISION_NETWORK" and (self.enableCoredumpMonitor or self.enableResourceMonitor):
            raise ValueError("设备监控仅支持海康网络设备资源")
        return self


class CoredumpMonitorOwner(Model):
    """资源共享监控的当前负责采集任务；只公开前端展示所需的稳定身份。"""

    id: str = Field(description='当前持有该设备 Core 监控租约的采集任务 ID。')
    name: str = Field(description='当前负责该设备 Core 监控的采集任务名称。')


class CoredumpMonitorStatus(Model):
    """资源级 Coredump 监控共享状态，不把短租约或设备控制配置回写给调用方。"""

    active: bool = Field(description='资源当前是否存在有效的 Coredump 监控负责人。')
    ownerTask: CoredumpMonitorOwner | None = Field(default=None, description='当前负责资源级 Coredump 监控的采集任务摘要。')
    mountStatus: str | None = Field(default=None, description='Coredump NFS 在设备侧最后一次观测到的挂载状态。')
