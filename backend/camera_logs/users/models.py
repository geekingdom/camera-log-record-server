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
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserCreate(Input):
    username: str = Field(pattern=r"^[a-zA-Z0-9_.-]{3,64}$")
    displayName: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=128)
    isAdmin: Literal[False] = False
    scopes: list[str] = Field(default_factory=list, max_length=32)
    enabled: bool = True

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
    version: int = Field(ge=1)
    displayName: str | None = Field(default=None, min_length=1, max_length=128)
    scopes: list[str] | None = Field(default=None, max_length=32)
    enabled: bool | None = None


class PasswordChange(Input):
    currentPassword: str = Field(min_length=1, max_length=128)
    newPassword: str = Field(min_length=8, max_length=128)


class PasswordReset(Input):
    version: int = Field(ge=1)
    password: str = Field(min_length=8, max_length=128)
