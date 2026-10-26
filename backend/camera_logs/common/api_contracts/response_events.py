"""审计、运行及请求事件响应合同；动态历史字段保留但不回显敏感正文。"""

from camera_logs.common.api_contracts.response_schema import array, field, obj

EVENT = obj(
    "经脱敏并补齐中文展示字段的事件。历史扩展字段会原样透传，但生产字段均在此明确列出。",
    {
        "id": field("string", "事件稳定标识。"),
        "createdAt": field("string", "事件在平台持久化或由历史记录迁移时保留的时间；具体设备或检测时间见 detectedAt 等专用字段。"),
        "summary": field("string", "中文短摘要。"),
        "outcome": field("string", "展示结果：PENDING 尚待完成，SUCCEEDED 成功，FAILED 失败，CANCELLED 已取消，UNKNOWN 无法由现有证据确认。"),
        "level": field("string", "DEBUG、INFO、WARNING 或 ERROR。"),
        "reason": field("string", "脱敏诊断原因。", nullable=True),
        "actor": field("string", "操作者或系统主体标识。", nullable=True),
        "actorName": field("string", "操作者展示名。", nullable=True),
        "sourceKind": field("string", "来源类别：SYSTEM、ANONYMOUS、BOOTSTRAP、USER、SERVICE_TOKEN、NODE、TASK_RUNTIME、SYSTEM_RUNTIME、UNAUTHENTICATED_HTTP、REQUEST_IDENTITY_MISSING 或 HISTORICAL。"),
        "sourceName": field("string", "主体来源展示名。"),
        "sourceDetail": field("string", "主体来源详情。", nullable=True),
        "metadata": obj("用户删除触发的资源或任务处置审计上下文；只保存归属用户标识。", {
            "createdBy": field("string", "处置前的资源或任务归属用户ID，即被删除的用户。"),
            "targetUserId": field("string", "转移后的接收用户ID；同步删除资源时省略。"),
        }, ("createdBy",)),
        "action": field("string", "审计动作代码。", nullable=True),
        "targetId": field("string", "受操作对象标识。", nullable=True),
        "targetName": field("string", "对象展示名。", nullable=True),
        "requestId": field("string", "关联 HTTP 请求追踪标识。", nullable=True),
        "serviceTokenId": field("string", "服务账号标识。", nullable=True),
        "clientIp": field("string", "客户端来源 IP。", nullable=True),
        "route": field("string", "HTTP 路由模板。", nullable=True),
        "method": field("string", "HTTP 方法。", nullable=True),
        "httpStatus": field("integer", "HTTP 响应状态码。", nullable=True),
        "responseComplete": field("boolean", "HTTP 响应是否已完整发送到 ASGI 发送层；false 时 outcome 为 UNKNOWN。", nullable=True),
        "responseBytes": field("integer", "HTTP 响应已发送字节数，单位字节；仅请求事件出现。", nullable=True),
        "durationMs": field("number", "请求耗时毫秒。", nullable=True),
        "type": field("string", "运行事件类型。", nullable=True),
        "phase": field("string", "事件阶段。", nullable=True),
        "status": field("string", "运行事件的原始状态代码；其枚举取决于 type，例如 COREDUMP_MOUNT 使用 MOUNTED、REMOUNTING、FAILED、UNMOUNTED、UNMOUNT_SKIPPED、UNMOUNT_FAILED。", nullable=True),
        "mode": field("string", "调试运行模式。", nullable=True),
        "debugDiagnostic": {
            "type": "object",
            "description": "调试模式诊断对象。",
            "nullable": True,
            "additionalProperties": field("string", "诊断阶段或安全文本值。"),
        },
        "taskId": field("string", "关联任务标识。", nullable=True),
        "taskName": field("string", "关联任务名称。", nullable=True),
        "resourceId": field("string", "关联设备资源标识。", nullable=True),
        "resourceName": field("string", "关联设备资源名称。", nullable=True),
        "deviceIp": field("string", "关联设备 IP。", nullable=True),
        "nodeId": field("string", "关联服务节点标识。", nullable=True),
        "nodeName": field("string", "关联服务节点名称。", nullable=True),
        "oldNodeId": field("string", "迁移前节点标识。", nullable=True),
        "newNodeId": field("string", "迁移后节点标识。", nullable=True),
        "runId": field("string", "关联采集运行标识。", nullable=True),
        "sessionId": field("string", "关联采集会话标识。", nullable=True),
        "fileId": field("string", "关联文件标识。", nullable=True),
        "fileName": field("string", "关联文件名称。", nullable=True),
        "diskPercent": field("number", "节点磁盘使用百分比。", nullable=True),
        "previousLevel": field("string", "DISK_PRESSURE_CHANGED 或 WRITE_PRESSURE_CHANGED 事件变化前的压力等级。", nullable=True),
        "writeLatencyMs": field("number", "写入延迟毫秒。", nullable=True),
        "writeLatencyLimitMs": field("number", "写入延迟准入上限毫秒。", nullable=True),
        "writeLatencySamples": field("integer", "延迟样本数。", nullable=True),
        "writeLatencyPendingMs": field("number", "待写入延迟毫秒。", nullable=True),
        "writeLatencyWindowSeconds": field("number", "延迟统计窗口秒数。", nullable=True),
        "detectedAt": field("string", "旧事件原始检测时间。", nullable=True),
        "evidence": field("string", "隔离或运行事件证据摘要。", nullable=True),
    },
    ("summary", "outcome", "level"),
)

# 以下字段来自实际事件写入器，而不是将历史未知字段包装成虚构容器。
EVENT["properties"].update(
    {
        "previousReceivedAt": field("string", "服务器时钟回拨前一块日志的接收时间。"),
        "receivedAt": field("string", "发生回拨的日志块实际接收时间。"),
        "sequence": field("integer", "回拨日志块的会话接收序号，顺序不因墙上时间变化而改变。"),
        "commandId": field("string", "未发送定时命令的配置标识。", nullable=True),
        "execution": field("integer", "该定时命令的执行尝试序号。", nullable=True),
        "commandBlocked": field("boolean", "调试交互是否阻止后续设备命令发送。"),
        "generation": field("integer", "从机引导所属的任务运行世代。"),
        "sshTarget": field("string", "从机引导的目标 HOST 或 SLAVE_1/2/3。", nullable=True),
        "port": field("integer", "从机 SSH 引导使用的端口。"),
        "hostTaskId": field("string", "协助引导的主机采集任务标识。", nullable=True),
        "hostNodeId": field("string", "主机采集任务所在节点。", nullable=True),
        "name": field("string", "首次发现的 Coredump 源文件名。"),
        "size": field("integer", "首次发现时的 Coredump 文件字节数。"),
        "version": field("integer", "首次发现时的 Coredump 目录版本。"),
        "targetKind": field("string", "请求作用的资源类别，例如 tasks、downloads。"),
        "targetLabel": field("string", "请求作用对象类别的中文名称。"),
        "targetScope": field("string", "OBJECT 单对象、COLLECTION 集合或 PLATFORM 平台级操作。"),
        "errorType": field("string", "请求处理异常的类型；正常请求为 null。", nullable=True),
        "errorFrames": array(
            "最多16个异常代码位置；正常请求为 null，不含源码及局部变量。",
            obj(
                "代码位置。",
                {
                    "file": field("string", "源码文件名，不含绝对路径。"),
                    "line": field("integer", "一基行号。"),
                    "function": field("string", "函数名称。"),
                },
                ("file", "line", "function"),
            ),
        )
        | {"nullable": True},
    }
)
EVENT["properties"]["debugDiagnostic"] = obj(
    "经过白名单过滤的调试与解密接口诊断；不包含凭据和成功解密正文。",
    {
        "stage": field("string", "失败或检查发生的交互阶段。"),
        "reason": field("string", "稳定的诊断原因代码。"),
        "elapsedMs": field("number", "该阶段耗时，毫秒。"),
        "endpoint": field("string", "已去除凭据、查询和片段的接口地址。"),
        "httpStatus": field("integer", "远端接口 HTTP 状态码。"),
        "serviceCode": field("string", "远端接口脱敏后的业务错误码。"),
        "message": field("string", "远端标准 message 字段的脱敏说明。"),
        "error_description": field("string", "远端标准 error_description 的脱敏说明。"),
        "error": field("string", "远端标准 error 的脱敏说明。"),
        "exceptionType": field("string", "本阶段异常类型。"),
        "exceptionMessage": field("string", "脱敏并裁剪后的异常说明。"),
        "causeType": field("string", "底层原因异常类型。"),
        "causeMessage": field("string", "底层异常的脱敏说明。"),
        "responseContentType": field("string", "非JSON错误响应的媒体类型。"),
        "responseBytes": field("integer", "错误响应原始字节数。"),
        "responsePreview": field("string", "限定512字符的脱敏错误预览。"),
        "responsePreviewTruncated": field("boolean", "错误预览是否被截断。"),
    },
    (),
) | {"nullable": True}

EVENT["properties"]["responseValidation"] = obj(
    "响应字段校验失败的安全诊断；其他异常或正常请求为 null，不包含实际响应值。",
    {
        "count": field("integer", "本次响应校验错误总数，包括超出展示上限的错误。"),
        "truncated": field("boolean", "错误超过20项或字段路径超过12层时为 true，表示诊断已截断。"),
        "issues": array("最多20项字段校验错误。", obj(
            "单项响应校验错误，不记录原始值和错误正文。",
            {
                "location": array("从 response 开始的字段路径；数组下标以字符串表示，未知字段或动态字典键显示占位符。",
                                  field("string", "合同字段名称、数组下标，或隐藏未知键的占位符。")),
                "type": field("string", "Pydantic 标准错误码，例如 missing 缺字段、extra_forbidden 未声明字段、string_type 字符串类型不符；自定义错误统一为 custom_error。"),
            }, ("location", "type"))),
    }, ("count", "truncated", "issues"),
) | {"nullable": True}

EVENT_PAGE = obj(
    "事件页码或游标分页结果。",
    {
        "items": array("当前页事件。", EVENT),
        "page": field("integer", "页码模式的当前页，从1开始。", nullable=True),
        "pageSize": field("integer", "本页最大条数。"),
        "total": field("integer", "匹配总数；游标默认不统计时为 null。", nullable=True),
        "nextCursor": field("string", "下一页游标；末页为 null。", nullable=True),
        "hasMore": field("boolean", "游标模式是否仍有下一页。", nullable=True),
    },
    ("items", "pageSize"),
)
