"""平台配置、看板和服务令牌的公开响应合同，逐字段说明业务语义。"""

from camera_logs.common.api_contracts.response_schema import array, field, obj

MONITOR_ITEM = obj(
    "从设备命令输出提取的一条指标定义。",
    {
        "id": field("string", "指标稳定标识，用于历史曲线对齐。"),
        "name": field("string", "指标展示名称。"),
        "command": field("string", "在采集连接上串行执行的单行命令。"),
        "pattern": field("string", "含数值捕获组的提取正则表达式。"),
        "unit": field("string", "原始采样单位 KB 或 %。"),
        "enabled": field("boolean", "是否采集此指标。"),
    },
    ("id", "name", "command", "pattern", "unit", "enabled"),
)
MONITOR = obj(
    "设备资源监控配置。",
    {
        "intervalSeconds": field("integer", "采样间隔秒数，固定 60。"),
        "retentionDays": field("integer", "资源指标保留天数，最多 90。"),
        "items": array("CPU 与内存指标定义。", MONITOR_ITEM),
        "processDiscoveryCommand": field("string", "发现进程的设备命令。"),
        "processRules": array(
            "进程匹配及命名规则。",
            obj(
                "进程规则。",
                {
                    "id": field("string", "进程规则稳定标识。"),
                    "name": field("string", "默认进程展示名称。"),
                    "pattern": field("string", "匹配进程输出的正则。"),
                    "nameGroup": field(
                        "integer", "用作动态名称的捕获组序号；null 使用固定名称。", nullable=True
                    ),
                    "enabled": field("boolean", "是否启用此进程规则。"),
                },
                ("id", "name", "pattern", "nameGroup", "enabled"),
            ),
        ),
        "processStatusCommand": field("string", "读取进程状态的单行命令模板，{pid} 替换为正整数进程号。"),
        "processValuePattern": field("string", "从进程状态提取 VmRSS 的正则，第一捕获组为 KB 数值。"),
    },
    (
        "intervalSeconds",
        "retentionDays",
        "items",
        "processDiscoveryCommand",
        "processRules",
        "processStatusCommand",
        "processValuePattern",
    ),
)
PLATFORM_SETTINGS = obj(
    "管理员平台设置。",
    {
        "retentionDays": field("integer", "原始日志及归档的存储保留天数。"),
        "clusterCapacity": field("integer", "集群任务容量上限。"),
        "liveLogBufferMiB": field("integer", "浏览器实时日志保留上限，单位 MiB。"),
        "serviceLogRetentionDays": field("integer", "API/Worker 运行日志保留天数。"),
        "logSegmentMiB": field("integer", "单个原始日志分片最大大小，单位 MiB；10 至 100，每 5 MiB 一档。Worker每秒刷新，刷新成功后新分片生效；已打开分片按原上限收尾；下载作业启动时固定该值。"),
        "resourceMonitor": MONITOR,
        "recordRetention": obj(
            "数据库增长记录保留策略；有效引用仍受保护。",
            {
                "auditDays": field("integer", "业务审计保留天数；0 关闭自动清理。"),
                "eventDays": field("integer", "运行事件保留天数；0 关闭自动清理。"),
                "runDays": field("integer", "已结束运行及终态明细保留天数；0 关闭维护。"),
            },
            ("auditDays", "eventDays", "runDays"),
        ),
        "version": field("integer", "乐观锁版本，更新时必须提交当前版本。"),
        "updatedAt": field("string", "最近配置更新时间。"),
    },
    (
        "retentionDays",
        "clusterCapacity",
        "liveLogBufferMiB",
        "serviceLogRetentionDays",
        "logSegmentMiB",
        "resourceMonitor",
        "recordRetention",
        "version",
        "updatedAt",
    ),
)
DISPLAY_SETTINGS = obj(
    "面向所有登录用户的显示配置。",
    {
        "liveLogBufferMiB": field("integer", "浏览器实时日志保留上限，单位 MiB。"),
    },
    ("liveLogBufferMiB",),
)
IP_POLICY = obj(
    "平台客户端来源白名单配置。",
    {
        "version": field("integer", "整体策略版本，用于并发更新校验。"),
        "enabled": field("boolean", "是否启用来源白名单。"),
        "clientIp": field("string", "服务端观察到的当前请求连接端点 IP。", nullable=True),
        "rules": array(
            "按来源网络限制已有身份权限的规则。",
            obj(
                "来源规则。",
                {
                    "label": field("string", "管理员填写的规则说明。"),
                    "network": field("string", "标准化 IPv4/IPv6 CIDR。"),
                    "scopes": array(
                        "此网络允许使用的权限。",
                        field("string", "权限作用域，不能给身份授予原本没有的权限。"),
                    ),
                },
                ("label", "network", "scopes"),
            ),
        ),
    },
    ("version", "enabled", "clientIp", "rules"),
)
TOKEN = obj(
    "服务令牌公开信息，不包含散列及密文。",
    {
        "id": field("string", "令牌稳定标识。"),
        "name": field("string", "管理员命名的服务账号名称。"),
        "userId": field("string", "绑定用户标识；有效权限实时跟随此用户。"),
        "version": field("integer", "令牌元数据和凭据的并发版本。"),
        "revoked": field("boolean", "令牌是否已撤销。"),
        "expiresAt": field("string", "令牌到期时间；null 表示无固定到期。", nullable=True),
        "createdAt": field("string", "令牌创建时间。"),
        "updatedAt": field("string", "最近轮换时间；从未轮换时可能省略。"),
        "effectiveStatus": field(
            "string", "ACTIVE、REVOKED、EXPIRED、USER_MISSING、USER_DELETED 或 USER_DISABLED。"
        ),
        "user": obj(
            "绑定用户摘要；用户不存在时省略。",
            {
                "id": field("string", "绑定用户标识。"),
                "username": field("string", "绑定用户登录名。"),
                "displayName": field("string", "绑定用户展示名称。", nullable=True),
                "enabled": field("boolean", "绑定用户是否启用。"),
                "deletedAt": field("string", "用户软删除时间；未删除时 null。", nullable=True),
            },
            ("id", "username", "displayName", "enabled", "deletedAt"),
        ),
    },
    ("id", "name", "userId", "version", "revoked", "expiresAt", "createdAt", "effectiveStatus"),
)
TOKEN_SECRET = TOKEN | {
    "properties": {
        **TOKEN["properties"],
        "token": field("string", "创建或轮换后本次响应返回的令牌明文；请妥善保存。"),
    },
    "required": [*TOKEN["required"], "token"],
}
RESOURCE_TOTAL = obj(
    "选定上海自然日的资源日志统计。",
    {
        "resourceId": field("string", "设备资源标识，历史统计可含已删除资源。"),
        "name": field("string", "设备名称；缺少名称时回退资源 ID。"),
        "ip": field("string", "设备资源 IP。", nullable=True),
        "bytes": field("integer", "原始日志分卷总字节，不是压缩后的物理占用。"),
        "fileCount": field("integer", "登记的有效日志分卷数。"),
    },
    ("resourceId", "name", "ip", "bytes", "fileCount"),
)
CORE_TOTAL = RESOURCE_TOTAL | {
    "description": "选定上海自然日的设备 Coredump 统计。",
    "properties": {
        **RESOURCE_TOTAL["properties"],
        "bytes": field("integer", "可见 Coredump 目录条目的总字节数。"),
        "fileCount": field("integer", "可见 Coredump 文件数量。"),
        "lastReceivedAt": field("string", "该设备最近一条 Coredump 的接收时间。", nullable=True),
    },
}
DASHBOARD = obj(
    "资源看板的一致时间快照。",
    {
        "generatedAt": field("string", "服务器生成此快照的时间。"),
        "date": field("string", "选定的上海自然日，YYYY-MM-DD。"),
        "days": field("integer", "趋势统计天数。"),
        "range": obj(
            "趋势覆盖日期区间，首尾都包含。",
            {
                "startDate": field("string", "趋势首日，YYYY-MM-DD。"),
                "endDate": field("string", "趋势末日，YYYY-MM-DD。"),
            },
            ("startDate", "endDate"),
        ),
        "overview": obj(
            "当前概览；资源与任务排除软删除记录。",
            {
                "resources": field("integer", "当前未删除设备数。"),
                "onlineResources": field("integer", "健康状态 ONLINE 的未删除设备数。"),
                "collectingTasks": field("integer", "期望 RUNNING 且实际 COLLECTING 的未删除任务数。"),
                "tasks": field("integer", "未删除且所属资源未删除的任务数。"),
                "nodes": field("integer", "未删除节点数；非管理员时 null。", nullable=True),
            },
            ("resources", "onlineResources", "collectingTasks", "tasks", "nodes"),
        ),
        "availability": obj(
            "当前身份可见的统计范围。",
            {
                "logs": field("boolean", "是否具有日志读取权限。"),
                "nodes": field("boolean", "是否可查看管理员节点摘要。"),
            },
            ("logs", "nodes"),
        ),
        "nodes": array(
            "管理员可见的节点摘要；非管理员时 null。",
            obj(
                "节点健康摘要。",
                {
                    "id": field("string", "节点稳定标识。"),
                    "name": field("string", "节点展示名。"),
                    "healthLevel": field("string", "HEALTHY、WARNING、CRITICAL、UNKNOWN 或 OFFLINE。"),
                    "reasons": array("健康判定原因。", field("string", "中文原因说明。")),
                    "accepting": field("boolean", "在线且当前可接收新任务。"),
                    "activeTasks": field("integer", "Worker 当前运行实例数。"),
                    "capacity": field("integer", "节点任务容量。", nullable=True),
                    "inputBytesPerSecond": field("number", "当前日志输入字节每秒。", nullable=True),
                    "lastHeartbeat": field("string", "最近数据库心跳时间。", nullable=True),
                },
                (),
            ),
        )
        | {"nullable": True},
        "notes": array("统计口径与限制说明。", field("string", "一条中文统计说明。")),
        "daily": array(
            "按上海自然日排列的日志趋势；无日志权限时 null。",
            obj(
                "一天的原始日志统计。",
                {
                    "date": field("string", "上海自然日，YYYY-MM-DD。"),
                    "bytes": field("integer", "登记原始日志分卷字节数。"),
                    "fileCount": field("integer", "登记原始日志分卷数量。"),
                },
                ("date", "bytes", "fileCount"),
            ),
        )
        | {"nullable": True},
        "topResources": array("选定日按原始日志字节降序排列的设备；无日志权限时 null。", RESOURCE_TOTAL)
        | {"nullable": True},
        "topResourcesTruncated": field(
            "boolean", "设备排行是否因数量上限截断；无日志权限时 null。", nullable=True
        ),
        "coredumps": obj(
            "选定日的 Coredump 目录统计；无日志权限时 null。",
            {
                "resourceCount": field("integer", "有 Coredump 的设备数量。"),
                "fileCount": field("integer", "可见 Coredump 文件总数。"),
                "bytes": field("integer", "可见 Coredump 总字节数。"),
                "items": array("按字节数排列的设备 Coredump 统计。", CORE_TOTAL),
                "truncated": field("boolean", "设备排行是否因数量上限截断。"),
            },
            ("resourceCount", "fileCount", "bytes", "items", "truncated"),
        )
        | {"nullable": True},
    },
    (
        "generatedAt",
        "date",
        "days",
        "range",
        "overview",
        "availability",
        "nodes",
        "notes",
        "daily",
        "topResources",
        "topResourcesTruncated",
        "coredumps",
    ),
)
