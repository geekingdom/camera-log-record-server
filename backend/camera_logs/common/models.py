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
    confirmIsolation: bool = Field(default=False, description='是否确认旧运行实例已完成外部隔离，仅管理员可设为 true。')
    evidence: str | None = Field(default=None, min_length=10, max_length=2000, description='确认旧实例隔离所依据的工单、操作记录或其他可核验证据。')

    @field_validator("evidence")
    @classmethod
    def nonblank_evidence(cls, value):
        """隔离依据不能只由空白组成，避免确认审计失去可核查内容。"""
        if value is not None and not value.strip():
            raise ValueError("隔离证据不能为空白")
        return value


class InitialCommand(Model):
    """描述一次发送到设备的初始或手动命令及其交互等待条件。"""
    command: str = Field(min_length=1, max_length=8192, description='发送给设备的一条单行命令正文，或管理员维护的全局快捷命令正文。')
    newline: Literal["\n", "\r\n", "\r"] = Field(default="\n", description='命令正文后追加的换行符：\\n 为 LF，\\r\\n 为 CRLF，\\r 为 CR；正文自身不可包含换行。')
    delaySeconds: float = Field(default=0, ge=0, le=3600, description='命令写入连接后额外等待的秒数；同会话后续命令在该等待结束后继续。')
    prompt: str | None = Field(default=None, max_length=256, description='命令发送后需要等待设备输出匹配的可选提示符。')
    timeoutSeconds: float = Field(default=30, gt=0, le=3600, description='启用提示符等待时允许等待设备响应的最长秒数。')

    @field_validator("command")
    @classmethod
    def valid_command(cls, value):
        """命令必须是单行文本，防止输入被拆分为意外的设备指令。"""
        if not value.strip() or any(char in value for char in ("\r", "\n", "\x00")):
            raise ValueError("命令必须为非空单行文本")
        return value


class CommonCommandCreate(Model):
    """管理员维护的全局手工命令提示项，和任务模板保持独立生命周期。"""

    command: str = Field(min_length=1, max_length=8192, description='发送给设备的一条单行命令正文，或管理员维护的全局快捷命令正文。')
    description: str = Field(default="", max_length=512, description='资源、任务、命令模板或常用命令的补充业务说明。')
    enabled: bool = Field(default=True, description='是否向所有用户的手工命令补全提供此命令；false 仅管理员可查看管理。')

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

    version: int = Field(ge=1, description='当前配置版本号，用于乐观锁并发修改校验。')
    command: str | None = Field(default=None, min_length=1, max_length=8192, description='发送给设备的一条单行命令正文，或管理员维护的全局快捷命令正文。')
    description: str | None = Field(default=None, max_length=512, description='资源、任务、命令模板或常用命令的补充业务说明。')
    enabled: bool | None = Field(default=None, description='是否向所有用户提供此命令；省略保持当前启用状态。')

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
    id: str = Field(default_factory=new_id, max_length=64, description='对象、命令配置或节点的稳定唯一标识。')
    totalExecutions: int = Field(default=1, gt=0, le=1_000_000, strict=True, description='该定时命令在一次任务运行中最多发起的发送次数。')
    intervalSeconds: int = Field(default=60, gt=0, le=31_536_000, strict=True, description='同一条定时命令两次发送之间等待的秒数。')


class CommandConfig(Model):
    """汇总任务或模板的连接后初始化命令和周期执行命令。"""
    initialCommands: list[InitialCommand] = Field(default_factory=list, max_length=100, description='连接建立后按协议初始化阶段通过该会话唯一发送队列执行的初始化命令配置；SSH主机和TELNET_DEVICE自动ASH为尽力行为，自动ASH失败不阻塞采集，SSH从机须先完成独立协议与目标身份确认。')
    scheduledCommands: list[ScheduledCommand] = Field(default_factory=list, max_length=100, description='按各自次数和间隔独立执行的定时命令配置列表。')


class TemplateCreate(CommandConfig):
    """创建命令模板时提交的名称、说明和完整命令配置。"""
    name: str = Field(min_length=1, max_length=128, description='资源、任务、模板、节点或服务账号的用户可识别名称。')
    description: str = Field(default="", max_length=2000, description='资源、任务、命令模板或常用命令的补充业务说明。')
    sharedWith: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=100, description='可读取此命令模板的指定有效用户标识列表。')
    sharedWithAll: bool = Field(default=False, description='是否将模板共享给全部有效平台用户，仅管理员可启用。')

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
    protocol: Literal["SSH", "TELNET_DEVICE", "TELNET_SERIAL"] = Field(description='采集协议：SSH 通过安全Shell连接，TELNET_DEVICE 登录网络设备，TELNET_SERIAL 读取串口服务器；串口任务不支持暂停。')
    sshTarget: Literal["HOST", "SLAVE_1", "SLAVE_2", "SLAVE_3"] = Field(default="HOST", description='SSH 日志采集目标：HOST 为默认主机；SLAVE_1、SLAVE_2、SLAVE_3 分别为指定从机。从机独立确认协议模式和目标身份；运行期间每60秒复用采集连接执行ifconfig，身份不匹配或无法确认时关闭连接并重新切换；不消耗用户定时命令预算。仅 SSH 可选择从机。')
    ip: str = Field(description='设备资源、串口服务器或采集连接目标的 IPv4/IPv6 地址。')
    port: int = Field(ge=1, le=65535, strict=True, description='SSH、Telnet 设备或串口服务器的连接端口。')
    username: str = Field(default="", max_length=256, description='登录设备或平台账户使用的用户名。')
    password: str = Field(default="", max_length=4096, description='用于登录设备、认证资源或创建账户的密码；普通查询不返回，仅专用凭据查看接口向有权限的资源所有者或管理员返回。')
    sourceTemplateId: str | None = Field(default=None, description='创建或编辑任务时复制命令配置的来源模板标识。')
    sourceTemplateVersion: int | None = Field(default=None, description='复制命令配置时记录的来源模板版本号。')
    resourceId: str = Field(min_length=1, max_length=64, description='所属设备资源的稳定唯一标识。')
    serialServerResourceId: str | None = Field(default=None, min_length=1, max_length=64, description='Telnet 串口任务可选关联的已登记串口服务器资源标识。')
    useResourceCredentials: bool | None = Field(default=None, description='SSH或Telnet设备任务是否在服务端复制所属海康资源的登录凭据；省略且用户名密码均未填写时默认启用。任务保存独立凭据副本。')
    autoStart: bool = Field(default=False, description='创建采集任务后是否立即提交启动操作。')
    encoding: str = Field(default="utf-8", description='接收设备输出时采用的字符编码名称。')
    loginPrompt: str = Field(default="login:", max_length=256, description='Telnet 登录阶段识别用户名输入提示符的文本。')
    passwordPrompt: str = Field(default="Password:", max_length=256, description='Telnet 登录阶段识别密码输入提示符的文本。')

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
    version: int = Field(ge=1, description='当前配置版本号，用于乐观锁并发修改校验。')
    name: str | None = Field(default=None, description='资源、任务、模板、节点或服务账号的用户可识别名称。')
    description: str | None = Field(default=None, description='资源、任务、命令模板或常用命令的补充业务说明。')
    protocol: Literal["SSH", "TELNET_DEVICE", "TELNET_SERIAL"] | None = Field(default=None, description='采集协议：SSH 通过安全Shell连接，TELNET_DEVICE 登录网络设备，TELNET_SERIAL 读取串口服务器；串口任务不支持暂停。')
    sshTarget: Literal["HOST", "SLAVE_1", "SLAVE_2", "SLAVE_3"] | None = Field(default=None, description='SSH 日志采集目标：HOST 为默认主机；SLAVE_1、SLAVE_2、SLAVE_3 分别为指定从机。从机独立确认协议模式和目标身份；运行期间每60秒复用采集连接执行ifconfig，身份不匹配或无法确认时关闭连接并重新切换；不消耗用户定时命令预算。仅 SSH 可选择从机。')
    ip: str | None = Field(default=None, description='设备资源、串口服务器或采集连接目标的 IPv4/IPv6 地址。')
    port: int | None = Field(default=None, description='SSH、Telnet 设备或串口服务器的连接端口。')
    username: str | None = Field(default=None, description='登录设备或平台账户使用的用户名。')
    password: str | None = Field(default=None, description='用于登录设备、认证资源或创建账户的密码；普通查询不返回，仅专用凭据查看接口向有权限的资源所有者或管理员返回。')
    clearPassword: bool = Field(default=False, description='编辑任务时是否明确清除已保存的设备登录密码。')
    initialCommands: list[InitialCommand] | None = Field(default=None, description='连接建立后按协议初始化阶段通过该会话唯一发送队列执行的初始化命令配置；SSH主机和TELNET_DEVICE自动ASH为尽力行为，自动ASH失败不阻塞采集，SSH从机须先完成独立协议与目标身份确认。')
    scheduledCommands: list[ScheduledCommand] | None = Field(default=None, description='按各自次数和间隔独立执行的定时命令配置列表。')
    sourceTemplateId: str | None = Field(default=None, description='创建或编辑任务时复制命令配置的来源模板标识。')
    sourceTemplateVersion: int | None = Field(default=None, description='复制命令配置时记录的来源模板版本号。')
    encoding: str | None = Field(default=None, description='接收设备输出时采用的字符编码名称。')
    loginPrompt: str | None = Field(default=None, description='Telnet 登录阶段识别用户名输入提示符的文本。')
    passwordPrompt: str | None = Field(default=None, description='Telnet 登录阶段识别密码输入提示符的文本。')
    serialServerResourceId: str | None = Field(default=None, min_length=1, max_length=64, description='Telnet 串口任务可选关联的已登记串口服务器资源标识。')


class TemplatePatch(TemplateCreate):
    """携带当前版本的完整模板内容，用于原子替换并推进版本号。"""
    version: int = Field(ge=1, description='当前配置版本号，用于乐观锁并发修改校验。')


class DownloadCreate(Model):
    """请求为指定任务和小时分片或上海时间范围创建可下载日志会话。"""
    taskId: str = Field(description='目标日志采集任务的稳定唯一标识。')
    hourIds: list[str] | None = Field(default=None, min_length=1, max_length=168, description='需要加入日志下载作业的逻辑小时归档标识列表，与start/end时间范围二选一；沿用小时目录返回的标识。')
    start: str | None = Field(default=None, description='按时间范围下载的开始时刻（包含），例如 2026-10-19T12:18:41；不带时区按北京时间解释，选中起始时刻所在小时。须与 end 同传，与 hourIds 二选一。')
    end: str | None = Field(default=None, description='按时间范围下载的结束时刻（不包含），例如 2026-10-19T15:10:08；不带时区按北京时间解释，向上取整为 16:00:00，整点值不再多取一小时。')
    allowPartial: bool = Field(default=False, description='小时归档存在缺失片段时，是否仍允许生成部分下载包。')

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
    taskId: str = Field(description='目标日志采集任务的稳定唯一标识。')
    keyword: str = Field(default="", max_length=1024, description='日志检索的字面关键词；空字符串按时间范围读取原始日志片段。')
    start: str | None = Field(default=None, description='时间范围开始时间，采用ISO 8601（不带时区按北京时间解释，也兼容显式时区），且包含该时刻。')
    end: str | None = Field(default=None, description='时间范围结束时间，采用ISO 8601（不带时区按北京时间解释，也兼容显式时区），且不包含该时刻。')


class TokenCreate(Model):
    """定义绑定既有用户的服务令牌名称和有效期，权限始终实时来自该用户。"""
    name: str = Field(min_length=1, max_length=128, description='资源、任务、模板、节点或服务账号的用户可识别名称。')
    userId: str = Field(min_length=1, max_length=64, description='服务账号绑定并实时继承权限的既有平台用户标识。')
    expiresInDays: int | None = Field(default=30, ge=1, le=365, description='服务账号从创建或更新起可使用的天数；null 表示永久有效。')


class TokenPatch(Model):
    """管理员以版本锁更新服务令牌名称、绑定用户或有效期，权限在下次认证实时切换。"""
    version: int = Field(ge=1, description='当前配置版本号，用于乐观锁并发修改校验。')
    name: str | None = Field(default=None, min_length=1, max_length=128, description='资源、任务、模板、节点或服务账号的用户可识别名称。')
    userId: str | None = Field(default=None, min_length=1, max_length=64, description='服务账号绑定并实时继承权限的既有平台用户标识。')
    expiresInDays: int | None = Field(default=None, ge=1, le=365, description='服务账号从创建或更新起可使用的天数；null 表示永久有效。')


class TokenRotate(Model):
    """管理员以当前版本轮换服务令牌凭据，不修改绑定关系或有效期。"""
    version: int = Field(ge=1, description='当前配置版本号，用于乐观锁并发修改校验。')
