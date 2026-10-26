"""资源、认证、指标和 Coredump 路由的真实公开响应合同。"""

from camera_logs.common.api_contracts.response_schema import array, field, obj, page

RESOURCE_TASK_SUMMARY = obj("资源详情内联的受限任务摘要，不等同于完整任务配置。", {
    "id": field("string", "任务稳定标识。"), "name": field("string", "任务展示名称。"),
    "protocol": field("string", "采集协议。"), "ip": field("string", "任务连接地址。"),
    "port": field("integer", "任务连接端口。"), "status": field("string", "当前任务状态。"),
    "desiredState": field("string", "任务期望状态。"), "resourceId": field("string", "关联设备资源标识。", nullable=True),
    "serialServerResourceId": field("string", "关联串口服务器资源标识。", nullable=True),
    "createdBy": field("string", "任务创建者标识。"), "createdByName": field("string", "任务创建者展示名。"),
}, ("id",))

RESOURCE = obj("设备资源公开详情；凭据密文、内部路径和资源监控租约不返回，现有投影会返回 Core 监控租约摘要。", {
    "id": field("string", "资源稳定标识。"), "name": field("string", "资源展示名称。"),
    "kind": field("string", "资源类型：HIKVISION_NETWORK 为海康网络设备，SERIAL_SERVER 为串口服务器。"), "ip": field("string", "规范化设备地址。"),
    "username": field("string", "网络设备登录用户名；不包含密码。"), "authType": field("string", "访问海康 ISAPI 的 HTTP 认证方式：DIGEST 或 BASIC；串口服务器通常省略。"),
    "model": field("string", "最近认证的设备型号。", nullable=True),
    "subSerialNumber": field("string", "最近认证的设备子序列号。", nullable=True),
    "softwareVersion": field("string", "最近认证的软件版本。", nullable=True),
    "createdBy": field("string", "资源创建者标识。"), "createdByName": field("string", "资源创建者展示名。"),
    "sharedUserIds": array("显式共享的有效平台用户ID；空列表不共享，权限仍受用户作用域限制，不能删除资源。", field("string", "指定共享用户ID。")),
    "version": field("integer", "资源乐观锁版本。"), "enableCoredumpMonitor": field("boolean", "是否启用 Core 监控。"),
    "enableResourceMonitor": field("boolean", "是否启用资源指标监控。"),
    "authenticatedAt": field("string", "最近成功认证时间。", nullable=True),
    "healthStatus": field("string", "最近网络设备认证状态：ONLINE 成功，AUTH_FAILED 凭据被拒绝，OFFLINE 无法连接，ERROR 为其它认证或协议错误；串口服务器可省略。", nullable=True),
    "healthCheckedAt": field("string", "最近一次网络设备健康认证完成时间；串口服务器可省略。", nullable=True),
    "healthMessage": field("string", "最近健康检查的脱敏说明，不含密码、响应正文或请求头。", nullable=True),
    "healthFailureCount": field("integer", "连续失败的网络设备健康认证次数；认证成功时归零。"), "healthRevision": field("integer", "健康状态 CAS 修订号，仅用于并发结果防覆盖。"),
    "authRejectedAt": field("string", "最近一次收到设备凭据拒绝（AUTH_FAILED）的时间；成功认证后清除。", nullable=True),
    "nextHealthCheckAt": field("string", "下一次周期健康检查不早于的时间。", nullable=True),
    "deletedAt": field("string", "资源进入软删除的时间；null 或省略表示仍正常可见。", nullable=True), "deletionState": field("string", "软删除收尾状态：PENDING 表示关联任务或连接仍待回收，DONE 表示回收完成。", nullable=True),
    "deletionCompletedAt": field("string", "软删除关联任务和连接回收完成时间；仅 deletionState=DONE 时出现。", nullable=True),
    "hasCoredumps": field("boolean", "是否存在可展示的 Core 文件。"),
    "taskCount": field("integer", "未删除关联任务总数。"), "activeTaskCount": field("integer", "正在采集的关联任务数。"),
    "unsettledTaskCount": field("integer", "仍待运行或回收的关联任务数。"),
    "tasks": array("最多 taskLimit 条内联任务摘要。", RESOURCE_TASK_SUMMARY),
    "tasksTruncated": field("boolean", "内联任务是否被 taskLimit 截断。"), "tasksUrl": field("string", "读取完整任务列表的相对地址。"),
    "coredumpLeaseTaskId": field("string", "当前 Core 监控租约任务标识。", nullable=True),
    "coredumpLeaseRunId": field("string", "当前 Core 监控租约运行标识。", nullable=True),
    "coredumpLeaseGeneration": field("integer", "当前 Core 监控租约运行世代。", nullable=True),
    "coredumpLeaseNodeId": field("string", "当前 Core 监控租约节点标识。", nullable=True),
    "coredumpLeaseUntil": field("string", "当前 Core 监控租约到期时间。", nullable=True),
    "coredumpLeaseTarget": field("string", "当前 Core 挂载目标。", nullable=True),
    "coredumpLeaseSourceNodeId": field("string", "登记 Core 挂载目标的节点标识。", nullable=True),
    "createdAt": field("string", "资源创建时间。"), "updatedAt": field("string", "资源最近更新时间。"),
    "controlClaimVersion": field("integer", "资源控制围栏版本；每次可能改变关联任务运行归属的控制事务递增，用于拒绝过期写入。", nullable=True),
}, ("id", "hasCoredumps", "taskCount", "activeTaskCount", "unsettledTaskCount", "tasks", "tasksTruncated", "tasksUrl"))

RESOURCE_LIST_PAGE = page(RESOURCE)

RESOURCE_CREATED = obj("批量创建后直接返回的资源记录；不含详情读取时计算的任务统计。", {
    key: value for key, value in RESOURCE["properties"].items()
    if key not in {"hasCoredumps", "taskCount", "activeTaskCount", "unsettledTaskCount", "tasks", "tasksTruncated", "tasksUrl"}
}, ("id", "name", "kind", "ip", "version", "createdAt", "updatedAt", "createdBy"))

AUTH_RECORD = obj("资源认证历史聚合段。", {
    "id": field("string", "认证历史标识。"), "resourceId": field("string", "资源标识。"),
    "createdAt": field("string", "本段首次认证完成时间。"), "latestAt": field("string", "本段最近认证完成时间。"),
    "utcDay": field("string", "认证聚合所在 UTC 自然日，YYYY-MM-DD；仅用于同日连续观测归并和历史定位。"),
    "occurrenceCount": field("integer", "连续同结果累计次数。"), "expiresAt": field("string", "认证历史保留到期时间。", nullable=True),
    "source": field("string", "CREATE、EDIT、MANUAL 或 PERIODIC。"), "result": field("string", "SUCCESS、AUTH_FAILED、OFFLINE 或 ERROR。"),
    "modelBefore": field("string", "认证前型号。", nullable=True), "modelAfter": field("string", "认证后型号。", nullable=True),
    "serialBefore": field("string", "认证前子序列号。", nullable=True), "serialAfter": field("string", "认证后子序列号。", nullable=True),
    "identityChanged": field("boolean", "认证是否识别出设备身份变化。"), "initialAuthentication": field("boolean", "是否首次成功认证。"),
    "message": field("string", "已脱敏认证说明。", nullable=True),
}, ("id", "resourceId", "createdAt"))

AUTH_RECORD_PAGE = obj("认证历史页码或游标分页结果。", {
    "items": array("当前页认证历史。", AUTH_RECORD), "total": field("integer", "页码请求的匹配总数；游标请求为 null。", nullable=True),
    "page": field("integer", "当前页码；游标请求固定为 1。"), "pageSize": field("integer", "请求的最大记录数。"),
    "hasMore": field("boolean", "游标请求是否还有下一页。"), "nextCursor": field("string", "下一页游标；末页为 null。", nullable=True),
}, ("items", "total", "page", "pageSize", "nextCursor"))

METRIC_VALUE = obj("单项已解析资源指标。", {
    "id": field("string", "指标标识。"), "name": field("string", "指标展示名称。"),
    "value": field("number", "采样数值。"), "unit": field("string", "指标单位。"),
}, ("id", "name", "value", "unit"))
METRIC_ERROR = obj("单项指标采集错误。", {
    "scope": field("string", "命令或指标范围。"), "code": field("string", "稳定错误码。"),
}, ("scope", "code"))
METRIC_SAMPLE = obj("资源监控采样。", {
    "sampledAt": field("string", "采样完成时间。"), "status": field("string", "采样结果：OK 全部已启用指标成功，PARTIAL 至少一项成功且至少一项失败，FAILED 本次未取得可用指标。"),
    "configVersion": field("integer", "采样时的平台监控配置版本。"),
    "identity": obj("采样时设备身份快照。", {"model": field("string", "设备型号。", nullable=True), "subSerialNumber": field("string", "设备子序列号。", nullable=True)}, ()),
    "values": array("成功采集的指标值。", METRIC_VALUE), "errors": array("局部采样错误。", METRIC_ERROR),
    "errorCode": field("string", "整次采样失败或部分失败的稳定错误码。", nullable=True),
}, ("sampledAt", "status", "configVersion", "values"))
METRIC_CONFIG = obj("公开资源监控配置。", {
    "version": field("integer", "平台配置版本。"), "intervalSeconds": field("integer", "采样间隔秒数。"),
    "retentionDays": field("integer", "历史保留天数。"), "items": array("指标定义。", obj("指标定义。", {
        "id": field("string", "指标标识。"), "name": field("string", "指标展示名称。"),
        "unit": field("string", "指标单位。"), "enabled": field("boolean", "是否启用。"),
    }, ("id", "name", "unit", "enabled"))),
}, ("version", "intervalSeconds", "retentionDays", "items"))
MONITOR_OWNER = obj("当前资源监控任务。", {"id": field("string", "任务标识。"), "name": field("string", "任务名称。")}, ("id", "name"))
METRIC_HISTORY = obj("资源指标游标页。", {
    "items": array("当前页指标采样。", METRIC_SAMPLE), "nextCursor": field("string", "下一页游标；末页为 null。", nullable=True),
    "owner": {**MONITOR_OWNER, "nullable": True}, "config": METRIC_CONFIG,
}, ("items", "nextCursor", "owner", "config"))

COREDUMP = obj("Coredump 文件公开目录项。", {
    "id": field("string", "文件标识；源文件最近扫描为 STABLE 且仍可读取时可直接用于下载。"), "kind": field("string", "目录文件类别；当前 Coredump 源文件为 COREDUMP。"),
    "status": field("string", "目录状态：RECEIVING 表示仅登记并持续观察源文件，FROZEN 表示已有冻结副本，RETIRING 表示副本清理等待既有读取结束。"), "resourceId": field("string", "所属资源标识。"),
    "nodeId": field("string", "保管节点标识。"), "name": field("string", "源文件名。"), "size": field("integer", "已观测文件大小。"),
    "receivedAt": field("string", "接收或目录记录时间。"), "firstSeenAt": field("string", "首次发现时间。"),
    "sourceModifiedAt": field("string", "最近观测到的源文件修改时间。", nullable=True), "sourceState": field("string", "源稳定性状态：OBSERVING 为首次或持续观察，CHANGING 表示发现版本变化，STABLE 表示同一版本已连续至少十秒未变化；源是否仍存在由下载接口再次确认。", nullable=True),
    "sourceObservedAt": field("string", "最近一次扫描观测源文件的时间。", nullable=True), "sourceUnchangedSince": field("string", "当前源版本连续未变化的观测起点。", nullable=True),
    "sourceStableAt": field("string", "扫描首次确认当前源版本稳定的时间；仅 sourceState=STABLE 时出现。", nullable=True), "version": field("integer", "同一源路径新版本递增的目录版本。"),
    "createdAt": field("string", "目录记录创建时间。", nullable=True), "updatedAt": field("string", "目录记录更新时间。", nullable=True),
    "expiresAt": field("string", "冻结副本到期时间。", nullable=True), "startedAt": field("string", "冻结开始时间。", nullable=True),
    "completedAt": field("string", "冻结完成时间。", nullable=True), "filename": field("string", "冻结副本文件名。", nullable=True),
    "bytes": field("integer", "冻结副本字节数。", nullable=True), "etag": field("string", "冻结副本 ETag。", nullable=True),
    "error": field("string", "冻结失败说明。", nullable=True),
}, ("id", "resourceId", "name"))
COREDUMP_PAGE = page(COREDUMP)
COREDUMP_MONITOR = obj("资源级 Coredump 监控当前归属。", {
    "active": field("boolean", "是否存在有效监控任务。"), "ownerTask": {**MONITOR_OWNER, "nullable": True},
    "mountStatus": field("string", "当前任务报告的挂载状态；无有效归属时为 null。", nullable=True),
}, ("active", "ownerTask", "mountStatus"))

EXPORT = obj("Coredump 导出异步作业公开状态。", {
    "id": field("string", "导出作业标识。"), "kind": field("string", "固定 COREDUMP_EXPORT。"),
    "status": field("string", "QUEUED、RUNNING、SUCCEEDED、FAILED 或 CANCELLED。"), "createdAt": field("string", "创建时间。"),
    "updatedAt": field("string", "最近状态更新时间。", nullable=True), "expiresAt": field("string", "导出产物到期时间。"),
    "startedAt": field("string", "开始冻结与压缩时间。", nullable=True), "completedAt": field("string", "终态完成时间。", nullable=True),
    "filename": field("string", "SUCCEEDED 时的 ZIP 文件名。", nullable=True), "bytes": field("integer", "SUCCEEDED 时的 ZIP 字节数。", nullable=True),
    "etag": field("string", "SUCCEEDED 时的产物版本标识。", nullable=True), "error": field("string", "FAILED 或 CANCELLED 时的错误说明。", nullable=True),
}, ("id", "kind", "status", "createdAt", "expiresAt"))
