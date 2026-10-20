"""严格校验账号输入，子账户不能通过作用域或角色字段提升为管理员。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PERMISSIONS = {
    "tasks:read": "查看设备与任务", "tasks:write": "编辑采集任务",
    "resources:create": "新增设备资源", "resources:write": "编辑和删除设备资源",
    "tasks:create": "新增采集任务",
    "tasks:control": "启停采集任务", "logs:read": "查看日志与 Coredump",
    "logs:download": "下载日志与 Coredump", "commands:send": "发送设备命令",
    "templates:read": "查看命令模板", "templates:write": "管理命令模板",
    "service-tokens:read": "查看本人服务账号",
}


class Input(BaseModel):
    """拒绝未知字段，避免角色或内部状态被客户端隐式写入。"""
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: str = Field(min_length=1, max_length=64, description='平台登录账号，服务端去除首尾空白并按小写匹配。')
    password: str = Field(min_length=1, max_length=128, description='平台账号登录密码，不是设备登录密码。')


class UserCreate(Input):
    username: str = Field(pattern=r"^[a-zA-Z0-9_.-]{3,64}$", description='登录设备或平台账户使用的用户名。')
    displayName: str = Field(min_length=1, max_length=128, description='平台账户在界面和审计记录中显示的名称。')
    password: str = Field(min_length=8, max_length=128, description='新平台账号的初始密码；普通查询不会返回密码或哈希。')
    isAdmin: Literal[False] = Field(default=False, description='账户是否为管理员；创建普通子账户时只能为 false。')
    scopes: list[str] = Field(default_factory=list, max_length=32, description='授予平台账号的权限标识列表；可选值由 GET /api/v1/users/permissions 返回，普通用户另有系统基础读取权限。')
    enabled: bool = Field(default=True, description='新平台账号是否可以登录和使用绑定的服务令牌。')

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value):
        """账号不区分 ASCII 大小写，唯一索引使用同一规范形式。"""
        return value.lower()

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, values):
        """只允许面向操作员的权限；admin 和通配符不属于子账户配置。"""
        if set(values) - PERMISSIONS.keys():
            raise ValueError("权限包含不支持的项目")
        return list(dict.fromkeys(values))

    @field_validator("displayName")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("显示名称不能为空")
        return value


class UserPatch(Input):
    version: int = Field(ge=1, description='当前配置版本号，用于乐观锁并发修改校验。')
    displayName: str | None = Field(default=None, min_length=1, max_length=128, description='平台账户在界面和审计记录中显示的名称。')
    scopes: list[str] | None = Field(default=None, max_length=32, description='授予平台账号的权限标识列表；可选值由 GET /api/v1/users/permissions 返回，普通用户另有系统基础读取权限。')
    enabled: bool | None = Field(default=None, description='是否允许该平台账号继续登录和使用绑定的服务令牌；省略保持原值。')


class PasswordChange(Input):
    currentPassword: str = Field(min_length=1, max_length=128, description='当前登录账户的原密码，用于验证本人改密请求。')
    newPassword: str = Field(min_length=8, max_length=128, description='要设置给当前登录账户的新密码。')


class PasswordReset(Input):
    version: int = Field(ge=1, description='当前配置版本号，用于乐观锁并发修改校验。')
    password: str = Field(min_length=8, max_length=128, description='管理员为目标平台账号设置的新密码；成功后该用户必须修改密码，已有会话失效。')
