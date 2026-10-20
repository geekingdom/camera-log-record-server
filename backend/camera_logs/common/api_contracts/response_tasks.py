"""任务、模板、常用命令和命令历史的公开响应字段合同。"""

from camera_logs.common.api_contracts.response_identity import COMMAND
from camera_logs.common.api_contracts.response_schema import array, field, obj
from camera_logs.common.api_contracts.response_task_runtime import TASK_RUNTIME_FIELDS

INITIAL_COMMAND = obj("连接完成后按数组顺序经同一发送队列执行的初始化命令。", {
    "command": field("string", "待写入设备的非空单行命令正文，最长 8192 个字符。"),
    "newline": field("string", "命令正文后实际写入的换行符，只会是 LF（\\n）、CRLF（\\r\\n）或 CR（\\r）。"),
    "delaySeconds": field("number", "本项命令写入完成、且配置 prompt 时已等到提示符后，在同一发送队列额外等待的秒数；范围 0 至 3600。"),
    "prompt": field("string", "写入后需要在设备输出中匹配的提示符；未配置时为 null，且不等待提示符。", nullable=True),
    "timeoutSeconds": field("number", "配置 prompt 后等待其出现的超时秒数；大于 0 且不超过 3600，未配置 prompt 时不触发此等待。"),
}, ("command", "newline", "delaySeconds", "timeoutSeconds"))
SCHEDULED_COMMAND = obj("每个采集运行独立计数的周期命令快照。", {
    "id": field("string", "该任务内定时命令的稳定标识；不同任务由模板复制后使用不同标识。"),
    "command": field("string", "待写入设备的非空单行命令正文，最长 8192 个字符。"),
    "newline": field("string", "命令正文后实际写入的换行符，只会是 LF（\\n）、CRLF（\\r\\n）或 CR（\\r）。"),
    "delaySeconds": field("number", "配置中保留的发送后等待秒数，范围 0 至 3600；当前定时发送循环以 intervalSeconds 排程，不会另行施加此等待。"),
    "prompt": field("string", "写入后需要在设备输出中匹配的提示符；未配置时为 null，且不等待提示符。", nullable=True),
    "timeoutSeconds": field("number", "配置 prompt 后等待其出现的超时秒数；大于 0 且不超过 3600，未配置 prompt 时不触发此等待。"),
    "totalExecutions": field("integer", "本采集运行内最多发送次数，范围 1 至 1000000；预算耗尽后不再发送。"),
    "intervalSeconds": field("integer", "首次发送前及每次后续发送前等待的整秒数，范围 1 至 31536000。"),
}, ("id", "command", "newline", "delaySeconds", "timeoutSeconds", "totalExecutions", "intervalSeconds"))

TASK = obj("任务公开配置和状态。", {
    "id": field("string", "任务稳定标识。"), "resourceId": field("string", "关联资源标识。", nullable=True),
    "name": field("string", "用户设置的任务名称。"),
    "protocol": field("string", "采集协议：SSH 连接安全 Shell；TELNET_DEVICE 登录网络设备；TELNET_SERIAL 读取串口服务器，且不支持暂停。"),
    "ip": field("string", "任务连接目标的规范 IPv4 或 IPv6 地址。"), "port": field("integer", "连接端口，范围 1 至 65535。"),
    "createdBy": field("string", "创建人标识。"), "createdByName": field("string", "创建人展示名。"),
    "description": field("string", "用户设置的任务说明，可为空字符串。"),
    "sshTarget": field("string", "仅 SSH 使用的日志目标：HOST 为主机，SLAVE_1、SLAVE_2、SLAVE_3 为指定从机；非 SSH 任务固定为 HOST。从机在首次连接及每 60 秒身份巡检中无法确认时会关闭并重新连接。"),
    "sharedWith": array("共享用户快照。", field("string", "用户标识。")), "sharedWithAll": field("boolean", "全员共享标志。"),
    "username": field("string", "设备用户名；密码不返回。"), "hasPassword": field("boolean", "是否已保存密码。"),
    "initialCommands": array("连接后按顺序执行的初始化命令。", INITIAL_COMMAND),
    "scheduledCommands": array("周期命令快照。", SCHEDULED_COMMAND),
    "sourceTemplateId": field("string", "复制命令配置时的来源模板标识；未从模板复制时为 null。", nullable=True),
    "sourceTemplateVersion": field("integer", "复制时记录的来源模板乐观版本；无来源模板时为 null。", nullable=True),
    "serialServerResourceId": field("string", "TELNET_SERIAL 可选绑定的串口服务器资源标识；未绑定时为 null。", nullable=True),
    "encoding": field("string", "服务端已验证可用、用于解码设备输出的字符编码名称。"),
    "loginPrompt": field("string", "登录提示符。"), "passwordPrompt": field("string", "密码提示符。"),
    "createdAt": field("string", "创建时间。"), "updatedAt": field("string", "更新时间。"),
    "version": field("integer", "任务配置的乐观并发版本；更新和删除需带回当前值。"), **TASK_RUNTIME_FIELDS,
}, ("id", "name"))

COMMON = obj("常用命令。", {
    "id": field("string", "命令标识。"), "command": field("string", "命令文本。"),
    "description": field("string", "用途说明。"), "enabled": field("boolean", "是否启用。"),
    "version": field("integer", "乐观并发版本。"), "createdAt": field("string", "创建时间。"),
    "updatedAt": field("string", "更新时间。"),
}, ("id", "command", "description", "enabled", "version"))
TEMPLATE = obj("命令模板及其独立命令配置。", {
    "id": field("string", "模板标识。"), "name": field("string", "模板名称。"), "description": field("string", "模板说明。"),
    "initialCommands": array("初始化命令。", INITIAL_COMMAND), "scheduledCommands": array("周期命令。", SCHEDULED_COMMAND),
    "sharedWith": array("共享用户。", field("string", "用户标识。")), "sharedWithAll": field("boolean", "全员共享。"),
    "createdBy": field("string", "创建人。"), "createdByName": field("string", "创建人展示名。"),
    "createdAt": field("string", "创建时间。"), "updatedAt": field("string", "更新时间。"),
    "deletedAt": field("string", "删除时间。", nullable=True), "version": field("integer", "乐观锁版本。"),
}, ("id", "name"))
COMMAND_HISTORY = obj("命令执行记录页及当前运行的定时预算。", {
    "items": array("当前页执行记录。", COMMAND), "total": field("integer", "匹配记录总数。"),
    "page": field("integer", "当前页码。"), "pageSize": field("integer", "页大小。"),
    "runId": field("string", "查询时任务运行标识。", nullable=True),
    "scheduledCommands": array("当前定时命令预算。", obj("定时预算。", {
        "id": field("string", "定时命令标识。"), "command": field("string", "当前命令文本。", nullable=True),
        "totalExecutions": field("integer", "本运行最多发送次数。"), "intervalSeconds": field("number", "当前配置的发送间隔秒数；历史快照缺失时为 null。", nullable=True),
        "attempts": field("integer", "当前运行已预留的发送次数，从 0 开始；预留结果未知时不会自动退回。"),
    }, ("id", "totalExecutions", "attempts"))),
}, ("items", "total", "page", "pageSize", "runId", "scheduledCommands"))
