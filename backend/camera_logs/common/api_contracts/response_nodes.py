"""节点配置、遥测及性能状态的完整公开返回字段释义。"""

from camera_logs.common.api_contracts.response_schema import array, field, obj

BUDGET_FIELDS = {
    "bufferMiB": field("integer", "应用采集缓冲预算，单位 MiB。"),
    "writeConcurrency": field("integer", "实时写入线程并发上限。"),
    "batchKiB": field("integer", "单次日志写入批量上限，单位 KiB。"),
    "flushIntervalMs": field("integer", "批量写入最长等待，单位毫秒。"),
    "readMiBPerSecond": field("integer", "后台归档和读取的总速率预算，单位 MiB/s。"),
}
BUDGET = obj("当前资源约束下的性能预算。", BUDGET_FIELDS, ())
PERFORMANCE_POLICY = obj(
    "管理员性能策略。",
    {
        "mode": field("string", "AUTO 自动调整或 MANUAL 手动预算。"),
        **BUDGET_FIELDS,
    },
    ("mode", *BUDGET_FIELDS),
)
RESOURCE_LIMITS = obj(
    "Worker 实际可用的宿主/容器资源上限。",
    {
        "effectiveCpu": field("number", "配额、亲和性和宿主共同限制的有效 CPU 核数。"),
        "memoryLimitBytes": field("integer", "有效内存上限，单位字节。"),
        "memoryAvailableBytes": field("integer", "当前可用内存，单位字节。"),
        "source": field("string", "资源限制的探测来源。"),
    },
    ("effectiveCpu", "memoryLimitBytes", "memoryAvailableBytes", "source"),
)
MEASUREMENT = obj(
    "有界阶段耗时统计。",
    {
        "samples": field("integer", "当前窗口样本数；0 表示尚无样本。"),
        "sampleWindow": field("integer", "最多保留的样本数量。"),
        "p99Ms": field("number", "样本的第 99 百分位耗时，单位毫秒。"),
    },
    ("samples", "sampleWindow", "p99Ms"),
)
IO_STATUS = obj(
    "实时写入线程池状态。",
    {
        "concurrency": field("integer", "当前有效写入并发上限。"),
        "active": field("integer", "正在执行的写入数。"),
        "waiting": field("integer", "等待写入槽的操作数。"),
        "queue": MEASUREMENT,
        "execution": MEASUREMENT,
        "stages": field("object", "按落盘阶段名称索引的耗时；键为阶段名。", additionalProperties=MEASUREMENT),
    },
    ("concurrency", "active", "waiting", "queue", "execution", "stages"),
)
ARCHIVES = obj(
    "后台归档排队和执行状态。",
    {
        "pendingJobs": field("integer", "所有尚未完成归档数，包含等待和处理中。"),
        "waitingJobs": field("integer", "等待压缩槽的归档数。"),
        "runningJobs": field("integer", "已取得压缩槽的归档数。"),
        "diskPendingJobs": field("integer", "最近磁盘清单扫描的未完成归档数。"),
        "oldestSeconds": field("number", "最老未完成归档自进入队列或最近磁盘 spool 观测起的年龄秒数。"),
        "oldestWaitingSeconds": field("number", "排队项的最长等待秒数。"),
        "oldestRunningSeconds": field("number", "处理中的最长执行秒数。"),
    },
    (),
)
PERFORMANCE_STATUS = obj(
    "Worker 自适应预算及实时压力状态；尚无报告时为 null。",
    {
        "effective": BUDGET,
        "baseline": BUDGET,
        "state": field("string", "NORMAL、THROTTLED、RECOVERING 或 MANUAL。"),
        "reason": field("string", "当前性能状态及预算变化原因。"),
        "changedAt": field("string", "预算或状态最近变化时间。", nullable=True),
        "resources": RESOURCE_LIMITS,
        "admissionBlocked": field("boolean", "资源安全约束是否禁止新任务准入。"),
        "budgetReduced": field("boolean", "有效预算是否低于当前基准。"),
        "pressureActive": field("boolean", "本次采样是否存在压力。"),
        "pressureSamples": field("integer", "连续压力采样次数。"),
        "recoveryStableSeconds": field("integer", "压力解除后连续稳定秒数。"),
        "buffer": obj(
            "应用预留缓冲，不包含协议库和操作系统缓存。",
            {
                "reservedBytes": field("integer", "已预留的采集缓冲字节数。"),
                "limitBytes": field("integer", "缓冲预算字节数。"),
                "oldestPendingMs": field("number", "最早尚未完成写入的等待毫秒数。"),
                "scope": field("string", "固定 APPLICATION_BATCH_RESERVATIONS，说明统计范围。"),
            },
            (),
        ),
        "writeIo": IO_STATUS,
        "archives": ARCHIVES,
        "writeIoLatency": obj(
            "实际写盘耗时，不含批量队列等待。",
            {
                "p99Ms": field("number", "写盘耗时第 99 百分位，毫秒。"),
                "pendingMs": field("number", "正在写盘的最长等待，毫秒。"),
                "samples": field("integer", "窗口内完成的写盘样本数。"),
                "windowSeconds": field("integer", "采样窗口秒数。"),
            },
            (),
        ),
    },
    (),
) | {"nullable": True}
TELEMETRY = obj(
    "资源遥测；过期时为无字段对象，未知值不代表零。",
    {
        "sampledAt": field("string", "按数据库心跳校准后的采样时间。", nullable=True),
        "workerSampledAt": field("string", "Worker 原始墙上时钟的采样时间。"),
        "scope": field("string", "HOST 宿主、RUNTIME 容器运行环境或 UNKNOWN。"),
        "status": field("string", "OK 或 UNKNOWN。"),
        "error": field("string", "遥测失败的异常类型，不包含敏感正文。"),
        "resourceLimits": RESOURCE_LIMITS,
        "cpuPercent": field("number", "CPU 使用率百分比；连续样本不足时 null。", nullable=True),
        "memoryUsedBytes": field("integer", "已使用内存字节数。"),
        "memoryTotalBytes": field("integer", "采样范围的总内存字节数。"),
        "memoryPercent": field("number", "内存使用率百分比。"),
        "networkUploadBytesPerSecond": field(
            "number", "外部网卡上传字节每秒；首样本或计数变化时 null。", nullable=True
        ),
        "networkDownloadBytesPerSecond": field(
            "number", "外部网卡下载字节每秒；首样本或计数变化时 null。", nullable=True
        ),
        "networkInterfaces": array("计入网络速率的网卡。", field("string", "网卡名称。")),
        "networkAccounting": field("string", "固定为 EXTERNAL_INTERFACES_EXCLUDING_LOOPBACK_AND_CONTAINER_BRIDGES，排除回环和容器桥接重复流量。"),
        "networkSampleSeconds": field("number", "网络速率两个有效样本之间的秒数。", nullable=True),
    },
    (),
)
RECOMMENDATION = obj(
    "只读性能建议；不会自动修改管理员策略。",
    {
        "available": field("boolean", "当前是否能提供可信建议。"),
        "reason": field("string", "推荐依据或不可用原因。"),
        "policy": PERFORMANCE_POLICY | {"nullable": True},
        "resources": RESOURCE_LIMITS | {"nullable": True},
        "reportedAt": field("string", "用于建议的节点心跳时间。", nullable=True),
    },
    ("available", "reason", "policy", "resources", "reportedAt"),
)
NODE_CONFIG_FIELDS = {
    "id": field("string", "Worker 稳定节点标识。"),
    "url": field("string", "管理员登记的节点服务地址；发现项为上报地址。"),
    "capacity": field("integer", "管理员配置或 Worker 上报的最大并发采集运行数，不是当前运行数。"),
    "accepting": field("boolean", "管理员准入开关或发现节点的实际准入状态。"),
    "version": field("integer", "管理配置乐观锁版本；未登记发现项为 0。"),
    "registered": field("boolean", "是否已有管理员登记配置。"),
    "inputRateLimitMiB": field("integer", "输入速率准入阈值，MiB/s；0 表示禁用。"),
    "writeLatencyLimitMs": field("integer", "写入延迟准入阈值，毫秒；超限阻止新会话。"),
    "isGeneralNode": field("boolean", "是否为允许一般资源接入的通用节点。"),
    "resourceNetworks": array("允许节点接入的资源地址或 CIDR 白名单；空数组时仅通用节点规则决定是否可分配。", field("string", "标准化 IP 地址或 CIDR 网段。")),
    "performance": PERFORMANCE_POLICY,
    "online": field("boolean", "以 API 当前时刻判断最近 30 秒内是否收到有效 Worker 心跳。"),
    "reportedAt": field("string", "最近实际心跳时间；无心跳时 null。", nullable=True),
    "performanceRecommendation": RECOMMENDATION,
    "reportedUrl": field("string", "Worker 自报地址；无报告时省略或 null。", nullable=True),
    "urlMismatch": field("boolean", "登记地址与 Worker 上报地址是否不同。"),
    "activeTasks": field("integer", "Worker 当前持有的运行实例数。"),
    "diskPercent": field("number", "日志存储所在磁盘使用率百分比。", nullable=True),
    "performanceStatus": PERFORMANCE_STATUS,
}
NODE_CONFIG = obj(
    "管理节点配置；缺少心跳的运行字段省略。",
    NODE_CONFIG_FIELDS,
    ("id", "url", "capacity", "accepting", "version", "registered"),
)
NODE_FIELDS = {
    "id": NODE_CONFIG_FIELDS["id"],
    "name": field("string", "节点展示名称，历史记录可能没有。"),
    "url": field("string", "Worker 实际公布的服务地址。"),
    "capacity": NODE_CONFIG_FIELDS["capacity"],
    "activeTasks": NODE_CONFIG_FIELDS["activeTasks"],
    "accepting": field("boolean", "结合容量、磁盘、延迟与隔离状态计算的实际准入开关。"),
    "inputRateLimitMiB": NODE_CONFIG_FIELDS["inputRateLimitMiB"],
    "writeLatencyLimitMs": NODE_CONFIG_FIELDS["writeLatencyLimitMs"],
    "diskPercent": NODE_CONFIG_FIELDS["diskPercent"],
    "diskFreeBytes": field("integer", "日志磁盘剩余字节数。"),
    "inputBytesPerSecond": field("number", "全部采集实例的输入字节每秒。", nullable=True),
    "heartbeat": field("string", "平台数据库接收到该节点最近心跳的服务器时间。"),
    "workerReportedAt": field("string", "Worker 发送心跳时自身时钟的时间。"),
    "assessedAt": field("string", "API 评估此快照的服务器时间。"),
    "telemetryAgeSeconds": field("number", "心跳发送时遥测样本的单调时钟年龄，秒。", nullable=True),
    "telemetry": TELEMETRY,
    "performanceStatus": PERFORMANCE_STATUS,
    "configurationMismatch": field("boolean", "Worker 实际上报地址是否与管理员登记地址不一致；未登记节点为 false。"),
    "configuredUrl": field("string", "管理员配置地址；未登记时 null。", nullable=True),
    "capabilities": obj(
        "节点功能能力。", {"coredumpNfs": field("boolean", "是否配置 Coredump NFS 接收能力。")}, ()
    ),
    "isolated": field("boolean", "是否已有管理员确认的外部隔离，确认后调度不会将新运行分配给该节点。"),
    "shuttingDownAt": field("string", "节点开始优雅关闭的时间；恢复心跳后移除，旧 Worker 可省略。"),
    "deletedAt": field("string", "节点配置删除时间；普通列表排除已删除项。", nullable=True),
    "health": obj(
        "API 评估的健康状态。",
        {
            "status": field("string", "健康结论：HEALTHY 正常，WARNING 接近阈值，CRITICAL 超过关键阈值，UNKNOWN 指标不足，OFFLINE 心跳过期。"),
            "reasons": array("健康判定的中文原因。", field("string", "一项原因。")),
        },
        ("status", "reasons"),
    ),
}
for prefix, label in (("writeLatency", "接收至写入总等待"), ("writeIoLatency", "实际写盘耗时")):
    NODE_FIELDS.update(
        {
            f"{prefix}Ms": field("number", f"{label}的窗口 P99 与尚未结束耗时两者最大值，毫秒。"),
            f"{prefix}Samples": field("integer", f"{label}的窗口样本数。"),
            f"{prefix}PendingMs": field("number", f"{label}的未完成操作最大等待，毫秒。"),
            f"{prefix}WindowSeconds": field("integer", f"{label}的统计窗口秒数。"),
        }
    )
NODE_FIELDS["writeIoLatencyP99Ms"] = field("number", "实际写盘已完成样本第 99 百分位，毫秒。")
NODE = obj("节点运行快照；旧版本 Worker 可缺少新增遥测字段。", NODE_FIELDS, ("id", "health", "assessedAt"))
