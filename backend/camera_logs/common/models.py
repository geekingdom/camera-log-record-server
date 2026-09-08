"""定义 API 输入模型，并在边界执行设备、命令和版本字段校验。"""

from ipaddress import ip_address
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def new_id():
    """生成数据库与 API 共同使用的无连字符随机标识。"""
    return uuid4().hex


class Model(BaseModel):
    """所有外部请求模型的基类，拒绝未声明字段以避免静默配置错误。"""
    model_config = ConfigDict(extra="forbid")


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

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        """去除模板名称两端空白，并拒绝仅由空白构成的名称。"""
        if not value.strip():
            raise ValueError("名称不能为空")
        return value.strip()


class TaskCreate(TemplateCreate):
    """创建采集任务时的设备连接参数、认证信息及可选模板来源快照。"""
    protocol: Literal["SSH", "TELNET_DEVICE", "TELNET_SERIAL"]
    ip: str
    port: int = Field(ge=1, le=65535, strict=True)
    username: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=4096)
    sourceTemplateId: str | None = None
    sourceTemplateVersion: int | None = None
    resourceId: str = Field(min_length=1, max_length=64)
    serialServerResourceId: str | None = Field(default=None, min_length=1, max_length=64)
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
        """SSH 与 Telnet 设备必须有账号密码；串口设备可不需要认证。"""
        if self.protocol != "TELNET_SERIAL" and (not self.username.strip() or not self.password):
            raise ValueError("SSH 和 Telnet 设备必须填写用户名和密码")
        return self


class TaskPatch(Model):
    """按乐观锁版本局部更新采集任务；未提供的字段保持原值。"""
    version: int = Field(ge=1)
    name: str | None = None
    description: str | None = None
    protocol: Literal["SSH", "TELNET_DEVICE", "TELNET_SERIAL"] | None = None
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
    """请求为指定任务和小时分片创建可下载日志会话。"""
    taskId: str
    hourIds: list[str] = Field(min_length=1, max_length=168)
    allowPartial: bool = False


class SearchCreate(Model):
    """定义任务日志检索关键词及可选的起止时间范围。"""
    taskId: str
    keyword: str = Field(min_length=1, max_length=1024)
    start: str | None = None
    end: str | None = None


class TokenCreate(Model):
    """定义服务令牌名称、授权作用域、可访问任务和有效期。"""
    name: str = Field(min_length=1, max_length=128)
    scopes: list[str]
    taskIds: list[str] | None = None
    expiresInDays: int = Field(default=30, ge=1, le=365)
