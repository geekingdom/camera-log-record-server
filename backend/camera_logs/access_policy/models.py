"""声明平台来源 IP 白名单规则，拒绝未知权限和不规范网络地址。"""

import ipaddress

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from camera_logs.users.models import PERMISSIONS

POLICY_SCOPES = frozenset((*PERMISSIONS, "admin", "*"))


class Model(BaseModel):
    """策略请求拒绝未声明字段，避免客户端写入内部状态。"""

    model_config = ConfigDict(extra="forbid")


class IpRule(Model):
    """一条平台客户端来源网络规则及其可使用的既有权限集合。"""

    label: str = Field(min_length=1, max_length=128, description='来源 IP 白名单规则的可读名称。')
    network: str = Field(min_length=1, max_length=64, description='允许访问平台的单个 IP 地址或 CIDR 网段。')
    scopes: list[str] = Field(min_length=1, max_length=32, description='来自本 IP 或网段的调用最多可使用的权限列表，与登录用户或服务账号自身权限取交集；不会额外授予账户原本没有的权限。')

    @field_validator("label")
    @classmethod
    def clean_label(cls, value):
        """保存去除首尾空白的标签，避免视觉相同的规则难以管理。"""
        value = value.strip()
        if not value:
            raise ValueError("规则标签不能为空")
        return value

    @field_validator("network")
    @classmethod
    def normalize_network(cls, value):
        """标准化 IPv4/IPv6 地址或 CIDR，单地址扩展为对应主机网络。"""
        try:
            return str(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise ValueError("网络地址必须是有效 IPv4 或 IPv6 CIDR") from exc

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, values):
        """IP 规则可保留管理员权限，但不能声明系统未知的作用域。"""
        if set(values) - POLICY_SCOPES:
            raise ValueError("权限包含不支持的项目")
        return list(dict.fromkeys(values))


class IpPolicyPatch(Model):
    """使用版本比较整体替换平台来源限制，防止并发覆盖。"""

    version: int = Field(ge=1, strict=True, description='当前配置版本号，用于乐观锁并发修改校验。')
    enabled: bool = Field(description='是否启用平台来源 IP 白名单；启用时至少须保留一条允许规则。')
    rules: list[IpRule] = Field(max_length=256, description='来源 IP 白名单规则列表，启用策略时至少包含一项。')

    @model_validator(mode="after")
    def enabled_requires_rules(self):
        """启用空白名单会拒绝全部平台访问，因此在写入前明确拒绝。"""
        if self.enabled and not self.rules:
            raise ValueError("启用 IP 白名单时至少需要一条规则")
        return self
