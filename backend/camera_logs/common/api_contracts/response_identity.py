"""账号白名单、命令执行与任务控制操作的公开字段释义。"""

from camera_logs.common.api_contracts.response_schema import array, field, obj

USER = obj("账号公开资料；不包含密码哈希、会话代次或令牌。", {
    "id": field("string", "用户稳定标识。"),
    "username": field("string", "登录用户名。"),
    "displayName": field("string", "用户展示名称。"),
    "isAdmin": field("boolean", "是否管理员。"),
    "builtin": field("boolean", "是否部署初始化的内置账号。"),
    "scopes": array("账号权限；登录响应会应用来源 IP 权限交集。", field("string", "权限标识。")),
    "enabled": field("boolean", "账号是否启用。"),
    "mustChangePassword": field("boolean", "是否必须先修改密码才能访问业务接口。"),
    "version": field("integer", "编辑和重置密码使用的乐观并发版本。"),
    "createdAt": field("string", "账号创建时间。"),
    "updatedAt": field("string", "最近修改时间。"),
    "deletedAt": field("string", "软删除时间；未删除为 null。", nullable=True),
}, ("id", "username", "displayName", "isAdmin", "builtin", "scopes", "enabled",
    "mustChangePassword", "version", "createdAt", "updatedAt", "deletedAt"))

COMMAND = obj("手动或定时命令执行记录；历史缺少的快照字段可能省略。", {
    "id": field("string", "本次执行的稳定标识。"),
    "taskId": field("string", "所属采集任务。"),
    "runId": field("string", "创建记录时绑定的采集运行；不能迁移到后继运行重发，历史记录可能省略。"),
    "sessionId": field("string", "创建记录时绑定的设备连接会话；历史记录可能省略。"),
    "actor": field("string", "手动命令的提交者标识；定时命令和历史记录可能省略。"),
    "kind": field("string", "MANUAL 为手动命令，SCHEDULED 为定时命令；历史记录可能省略。"),
    "command": field("string", "执行时的命令正文快照；旧记录可能缺失或为 null。", nullable=True),
    "newline": field("string", "命令正文后实际写入的换行符；历史记录可能省略。"),
    "delaySeconds": field("number", "手动命令写入完成、且 prompt 已匹配后在同一队列等待的秒数，范围 0 至 3600；历史记录可能省略。"),
    "prompt": field("string", "写入后需要匹配的设备输出提示符；未配置时为 null。", nullable=True),
    "timeoutSeconds": field("number", "配置 prompt 后等待其出现的超时秒数，大于 0 且不超过 3600；历史记录可能省略。"),
    "commandId": field("string", "定时命令配置标识。"),
    "commandSource": field("string", "执行历史中的正文来源：SNAPSHOT 原快照、CURRENT_CONFIGURATION 当前同 ID 配置、UNAVAILABLE 不可恢复。"),
    "status": field("string", "QUEUED 排队、SENDING 发送中、SENT 已发送、SUCCEEDED 成功、FAILED 失败、CANCELLED 取消、UNKNOWN 结果未知且不可自动重发；历史记录可能省略。"),
    "createdAt": field("string", "执行记录创建时间。"),
    "startedAt": field("string", "手动命令原子领取、进入 SENDING 的时间。"),
    "completedAt": field("string", "本次执行终态写回时间。"),
    "error": field("string", "脱敏后的失败原因；成功或无附加原因时为 null。", nullable=True),
    "attempt": field("integer", "本运行中该定时配置已占用的尝试序号，从 1 开始。"),
    "totalExecutions": field("integer", "定时配置的最大执行次数快照。"),
    "intervalSeconds": field("integer", "定时配置的整秒执行间隔，范围 1 至 31536000；手动命令或旧快照为 null。", nullable=True),
}, ("id", "taskId"))

OPERATION = obj("异步任务控制操作；接收控制意图不表示设备状态已到达。", {
    "id": field("string", "操作稳定标识，可用于查询后续状态。"),
    "taskId": field("string", "被控制任务的标识。"),
    "status": field("string", "PENDING 等待执行、SUCCEEDED 状态已到达、FAILED 失败、CANCELLED 被后续意图取消。"),
    "desiredState": field("string", "目标意图：RUNNING、STOPPED 或 PAUSED；历史操作可能省略。"),
    "action": field("string", "操作来源与类型，如 start、stop、pause、resume、resume-wait-device、restart-blocked 或 system-resource-health-stop。"),
    "actor": field("string", "提交者标识；系统操作为 system。"),
    "createdAt": field("string", "控制意图创建时间。"),
    "updatedAt": field("string", "恢复阶段最近更新时间；未发生阶段更新时省略。"),
    "completedAt": field("string", "进入终态的时间；待处理时省略。"),
    "phase": field("string", "额外恢复阶段；ISOLATION_REQUIRED 表示需确认旧运行已隔离。"),
    "error": field("string", "控制失败原因；仅存在错误信息时返回。", nullable=True),
}, ("id", "taskId", "status"))
