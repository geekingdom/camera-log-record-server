"""定义 API 输入模型，并在边界执行设备、命令和版本字段校验。"""

from ipaddress import ip_address
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def new_id():
    """生成数据库与 API 共同使用的无连字符随机标识。"""
    return uuid4().hex


class Model(BaseModel):
    """所有外部请求模型的基类，拒绝未声明字段以避免静默配置错误。"""
    model_config = ConfigDict(extra="forbid")


class BlockedRestart(Model):
    """请求恢复一个关闭结果未确认的阻塞采集任务。"""
    confirmIsolation: bool = False
    evidence: str | None = Field(default=None, min_length=10, max_length=2000)

    @field_validator("evidence")
    @classmethod
    def nonblank_evidence(cls, value):
        """隔离依据不能只由空白组成，避免确认审计失去可核查内容。"""
        if value is not None and not value.strip():
            raise ValueError("隔离证据不能为空白")
        return value


class InitialCommand(Model):
    """描述一次发送到设备的初始或手动命令及其交互等待条件。"""
    command: str = Field(min_length=1, max_length=8192)
    newline: Literal["\n", "\r\n", "\r"] = "\n"
    delaySeconds: float = Field(default=0, ge=0, le=3600)
    prompt: str | None = Field(default=None, max_length=256)
    timeoutSeconds: float = Field(default=30, gt=0, le=3600)

    @field_validator("command")
    @classmethod
    def valid_command(cls, value):
        """命令必须是单行文本，防止输入被拆分为意外的设备指令。"""
        if not value.strip() or any(char in value for char in ("\r", "\n", "\x00")):
            raise ValueError("命令必须为非空单行文本")
        return value


class CommonCommandCreate(Model):
    """管理员维护的全局手工命令提示项，和任务模板保持独立生命周期。"""

    command: str = Field(min_length=1, max_length=8192)
    description: str = Field(default="", max_length=512)
    enabled: bool = True

    @field_validator("command")
    @classmethod
    def normalize_command(cls, value):
        """快捷命令必须为非空单行，并统一首尾空白以保证全局唯一键稳定。"""
        value = value.strip()
        if not value or any(char in value for char in ("\r", "\n", "\x00")):
            raise ValueError("命令必须为非空单行文本")
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value):
        """说明只用于补全提示，移除无意义的首尾空白但允许为空。"""
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError("命令描述必须为单行文本")
        return value.strip()


class CommonCommandPatch(Model):
    """以乐观锁局部修改全局快捷命令，禁用也是一次可审计的编辑。"""

    version: int = Field(ge=1)
    command: str | None = Field(default=None, min_length=1, max_length=8192)
    description: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None

    @field_validator("command")
    @classmethod
    def normalize_command(cls, value):
        """编辑时保持与创建相同的单行和唯一化规则。"""
        return CommonCommandCreate(command=value).command if value is not None else None

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value):
        """编辑说明仍不接受控制字符，避免提示列表出现多行内容。"""
        return CommonCommandCreate(command="command", description=value).description if value is not None else None

    @model_validator(mode="after")
    def has_changes(self):
        """空 PATCH 没有业务含义，提前拒绝避免无效版本递增和审计。"""
        if self.command is None and self.description is None and self.enabled is None:
            raise ValueError("至少需要修改命令、描述或启用状态之一")
        return self


class ScheduledCommand(InitialCommand):
    """在基础命令字段上增加计划任务标识、执行次数和间隔。"""
    id: str = Field(default_factory=new_id, max_length=64)
    totalExecutions: int = Field(default=1, gt=0, le=1_000_000, strict=True)
    intervalSeconds: int = Field(default=60, gt=0, le=31_536_000, strict=True)


class CommandConfig(Model):
    """汇总任务或模板的连接后初始化命令和周期执行命令。"""
    initialCommands: list[InitialCommand] = Field(default_factory=list, max_length=100)
    scheduledCommands: list[ScheduledCommand] = Field(default_factory=list, max_length=100)


class TemplateCreate(CommandConfig):
    """创建命令模板时提交的名称、说明和完整命令配置。"""
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    sharedWith: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=100)
    sharedWithAll: bool = False

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        """去除模板名称两端空白，并拒绝仅由空白构成的名称。"""
        if not value.strip():
            raise ValueError("名称不能为空")
        return value.strip()

    @field_validator("sharedWith")
    @classmethod
    def unique_shared_users(cls, value):
        """共享对象按用户 ID 去重并拒绝空白值，避免同一权限在文档中重复保存。"""
        normalized = [identifier.strip() for identifier in value]
        if any(not identifier for identifier in normalized):
            raise ValueError("共享用户 ID 不能为空")
        return list(dict.fromkeys(normalized))


class TaskCreate(TemplateCreate):
    """创建采集任务时的设备连接参数、认证信息及可选模板来源快照。"""
    protocol: Literal["SSH", "TELNET_DEVICE", "TELNET_SERIAL"]
    sshTarget: Literal["HOST", "SLAVE_1", "SLAVE_2", "SLAVE_3"] = "HOST"
    ip: str
    port: int = Field(ge=1, le=65535, strict=True)
    username: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=4096)
    sourceTemplateId: str | None = None
    sourceTemplateVersion: int | None = None
    resourceId: str = Field(min_length=1, max_length=64)
    serialServerResourceId: str | None = Field(default=None, min_length=1, max_length=64)
    useResourceCredentials: bool | None = None
    autoStart: bool = False
    encoding: str = "utf-8"
    loginPrompt: str = Field(default="login:", max_length=256)
    passwordPrompt: str = Field(default="Password:", max_length=256)

    @field_validator("ip")
    @classmethod
    def valid_ip(cls, value):
        """解析并规范化 IPv4 或 IPv6 地址，非法地址在 API 边界拒绝。"""
        return str(ip_address(value))

    @field_validator("encoding")
    @classmethod
    def valid_encoding(cls, value):
        """确认 Python 支持所选日志解码编码，避免运行时采集失败。"""
        import codecs
        codecs.lookup(value)
        return value

    @model_validator(mode="after")
    def credentials(self):
        """SSH 与 Telnet 设备必须有账号密码，且从机目标只由 SSH 使用。"""
        if self.protocol != "TELNET_SERIAL" and self.useResourceCredentials is None:
            # 兼容既有 API：显式传入口令继续创建独立任务；只有两项都省略时才默认复用资源。
            self.useResourceCredentials = not (self.username.strip() or self.password)
        if self.protocol != "TELNET_SERIAL" and not self.useResourceCredentials \
                and (not self.username.strip() or not self.password):
            raise ValueError("SSH 和 Telnet 设备必须填写用户名和密码")
        if self.protocol != "SSH" and self.sshTarget != "HOST":
            raise ValueError("仅 SSH 任务可选择从机日志采集目标")
        return self


class TaskPatch(Model):
    """按乐观锁版本局部更新采集任务；未提供的字段保持原值。"""
    version: int = Field(ge=1)
    name: str | None = None
    description: str | None = None
    protocol: Literal["SSH", "TELNET_DEVICE", "TELNET_SERIAL"] | None = None
    sshTarget: Literal["HOST", "SLAVE_1", "SLAVE_2", "SLAVE_3"] | None = None
    ip: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    clearPassword: bool = False
    initialCommands: list[InitialCommand] | None = None
    scheduledCommands: list[ScheduledCommand] | None = None
    sourceTemplateId: str | None = None
    sourceTemplateVersion: int | None = None
    encoding: str | None = None
    loginPrompt: str | None = None
    passwordPrompt: str | None = None
    serialServerResourceId: str | None = Field(default=None, min_length=1, max_length=64)


class TemplatePatch(TemplateCreate):
    """携带当前版本的完整模板内容，用于原子替换并推进版本号。"""
    version: int = Field(ge=1)


class DownloadCreate(Model):
    """请求为指定任务和小时分片或上海时间范围创建可下载日志会话。"""
    taskId: str
    hourIds: list[str] | None = Field(default=None, min_length=1, max_length=168)
    start: str | None = None
    end: str | None = None
    allowPartial: bool = False

    @model_validator(mode="after")
    def selected_hours_or_range(self):
        """下载只能按明确小时或完整时间范围二选一，避免缺口语义不确定。"""
        has_hours = self.hourIds is not None
        has_range = self.start is not None or self.end is not None
        if has_hours == has_range:
            raise ValueError("必须且只能提供 hourIds 或完整的 start/end 时间范围")
        if has_range and (self.start is None or self.end is None):
            raise ValueError("start 和 end 必须同时提供")
        return self


class SearchCreate(Model):
    """关键词为空时按时间返回原始片段；非空时执行既有字面关键词检索。"""
    taskId: str
    keyword: str = Field(default="", max_length=1024)
    start: str | None = None
    end: str | None = None


class TokenCreate(Model):
    """定义绑定既有用户的服务令牌名称和有效期，权限始终实时来自该用户。"""
    name: str = Field(min_length=1, max_length=128)
    userId: str = Field(min_length=1, max_length=64)
    expiresInDays: int | None = Field(default=30, ge=1, le=365)


class TokenPatch(Model):
    """管理员以版本锁更新服务令牌名称、绑定用户或有效期，权限在下次认证实时切换。"""
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    userId: str | None = Field(default=None, min_length=1, max_length=64)
    expiresInDays: int | None = Field(default=None, ge=1, le=365)


class TokenRotate(Model):
    """管理员以当前版本轮换服务令牌凭据，不修改绑定关系或有效期。"""
    version: int = Field(ge=1)
