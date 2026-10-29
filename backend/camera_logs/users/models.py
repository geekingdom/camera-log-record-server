"""严格校验账户角色、额外权限和日期输入，超级管理员不允许通过接口创建。"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class PermissionExpiry(Input):
    """权限截止日仅接受日期；null 明确表示长期有效。"""
    permissionsExpiresOn: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="额外权限有效截止日期，格式 YYYY-MM-DD，例如 2026-10-13；北京时间当天结束后收回额外权限，null 表示长期有效。")

    @field_validator("permissionsExpiresOn")
    @classmethod
    def validate_expiry(cls, value):
        """拒绝时间戳和非规范日期，避免日期被浏览器时区偏移。"""
        if value is not None and (len(value) != 10 or date.fromisoformat(value).isoformat() != value):
            raise ValueError("权限截止日期必须为 YYYY-MM-DD")
        return value


class UserCreate(PermissionExpiry):
    username: str = Field(pattern=r"^[a-zA-Z0-9_.-]{3,64}$", description='登录设备或平台账户使用的用户名。')
    displayName: str = Field(min_length=1, max_length=128, description='平台账户在界面和审计记录中显示的名称。')
    password: str = Field(min_length=8, max_length=128, description='新平台账号的初始密码；普通查询不会返回密码或哈希。')
    role: Literal['USER', 'ADMIN'] = Field(default='USER', description='配置角色：USER 普通用户（默认），ADMIN 管理员；不能创建超级管理员。')
    isAdmin: bool | None = Field(default=None, description='兼容旧客户端的管理员标记；推荐使用 role，同时传入时必须一致。')

    @model_validator(mode='after')
    def resolve_role(self):
        """兼容历史布尔输入，但拒绝与明确角色冲突。"""
        if self.isAdmin is not None:
            legacy = 'ADMIN' if self.isAdmin else 'USER'
            if 'role' in self.model_fields_set and self.role != legacy:
                raise ValueError('role 与 isAdmin 不一致')
            self.role = legacy
        return self
    scopes: list[str] = Field(default_factory=list, max_length=32, description='授予平台账号的权限标识列表；可选值由 GET /api/v1/users/permissions 返回，固定基础权限由权限目录 fixed 标记，不受此列表和有效期限制。')
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


class UserPatch(PermissionExpiry):
    role: Literal["USER", "ADMIN"] | None = Field(default=None, description="要设置的配置角色：USER 普通用户，ADMIN 管理员；省略不变，不允许 null 或超级管理员。")
    version: int = Field(ge=1, description='当前配置版本号，用于乐观锁并发修改校验。')
    displayName: str | None = Field(default=None, min_length=1, max_length=128, description='平台账户在界面和审计记录中显示的名称。')
    scopes: list[str] | None = Field(default=None, max_length=32, description='授予平台账号的权限标识列表；可选值由 GET /api/v1/users/permissions 返回，固定基础权限由权限目录 fixed 标记，不受此列表和有效期限制。')
    enabled: bool | None = Field(default=None, description='是否允许该平台账号继续登录和使用绑定的服务令牌；省略保持原值。')


class PasswordChange(Input):
    currentPassword: str = Field(min_length=1, max_length=128, description='当前登录账户的原密码，用于验证本人改密请求。')
    newPassword: str = Field(min_length=8, max_length=128, description='要设置给当前登录账户的新密码。')


class PasswordReset(Input):
    version: int = Field(ge=1, description='当前配置版本号，用于乐观锁并发修改校验。')
    password: str = Field(min_length=8, max_length=128, description='管理员为目标平台账号设置的新密码；成功后该用户必须修改密码，已有会话失效。')
