"""定义设备资源接口的输入模型，并在请求边界校验设备类型与凭据。"""

from ipaddress import ip_address
from typing import Literal

from pydantic import Field, field_validator, model_validator

from camera_logs.common.models import Model


class ResourceInput(Model):
    """描述一个可供任务引用的网络设备或串口服务器资源。"""

    name: str = Field(min_length=1, max_length=128)
    kind: Literal["HIKVISION_NETWORK", "SERIAL_SERVER"]
    ip: str
    username: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=4096)
    authType: Literal["DIGEST", "BASIC"] = "DIGEST"

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        """名称去除首尾空白，避免视觉相同的空白资源名进入数据库。"""
        if not value.strip():
            raise ValueError("名称不能为空")
        return value.strip()

    @field_validator("ip")
    @classmethod
    def valid_ip(cls, value):
        """资源地址必须为规范的 IPv4 或 IPv6 文字地址。"""
        return str(ip_address(value))

    @model_validator(mode="after")
    def validate_kind_credentials(self):
        """网络设备需要完整凭据，串口服务器只允许名称和地址配置。"""
        if self.kind == "HIKVISION_NETWORK" and (not self.username.strip() or not self.password):
            raise ValueError("海康网络设备必须填写用户名和密码")
        if self.kind == "SERIAL_SERVER" and (self.username or self.password):
            raise ValueError("串口服务器不支持用户名或密码")
        return self


class ResourcePatch(Model):
    """更新资源展示名称或 HTTP 凭据时提交的完整稳定身份与乐观锁版本。"""

    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128)
    kind: Literal["HIKVISION_NETWORK", "SERIAL_SERVER"]
    ip: str
    username: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=4096)
    authType: Literal["DIGEST", "BASIC"] = "DIGEST"

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
        return self


class CoredumpMonitorOwner(Model):
    """资源共享监控的当前负责采集任务；只公开前端展示所需的稳定身份。"""

    id: str
    name: str


class CoredumpMonitorStatus(Model):
    """资源级 Coredump 监控共享状态，不把短租约或设备控制配置回写给调用方。"""

    active: bool
    ownerTask: CoredumpMonitorOwner | None = None
    mountStatus: str | None = None
