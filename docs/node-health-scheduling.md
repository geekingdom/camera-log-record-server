# 节点健康与动态分配

服务节点看板展示真实采样的 CPU、内存、网络上下行、任务容量、日志输入和磁盘写入情况。健康状态与原因由 API 统一计算。无数据、首个差分样本及过期指标显示未知，不能解释为零负载。

## 采样与部署

Worker 的资源采样独立于日志接收和心跳，CPU 使用两次 CPU 时间差，网络使用两次累计字节数差。接口统计排除回环及常见容器内部桥接接口。采样异常或超时不应中断采集；上一采样尚未结束时不启动重叠采样。

Docker Worker 以只读方式挂载 `/proc:/host/proc:ro`，并设置 `HOST_PROC_ROOT=/host/proc`，无需 privileged。CPU、内存和 `/host/proc/1/net/dev` 来自主机视图。Linux 原生部署默认直接采样运行主机。Docker Desktop 的主机指其 Linux 虚拟机，并非 macOS 宿主。权限不足或视图不可用时，页面必须以实际采样范围和未知状态解释结果。

`GET /api/v1/nodes` 的节点增加 `telemetry` 和 `health`，保留既有字段。`telemetry.sampledAt` 为 UTC 时间，`scope` 为 HOST/RUNTIME/UNKNOWN；包含 `cpuPercent`、`memoryUsedBytes`、`memoryTotalBytes`、`memoryPercent`、`networkUploadBytesPerSecond`、`networkDownloadBytesPerSecond`。百分比与字节速率缺失时为 null。网络是节点流量，不仅是采集流量；日志输入另看 `inputBytesPerSecond`。

网络API速率单位保持B/s，看板上行/下行换算为十进制Mb/s，与MobaXterm相同单位直接比较；采集输入仍按KiB/s、MiB/s显示。`10 Mb/s = 1,250,000 B/s = 1,220.7 KiB/s ≈ 1.19 MiB/s`，不是10MB/s。悬浮网络速率可查看等效字节速率、`networkInterfaces`实际累计网卡以及`networkSampleSeconds`实际差分窗口。采样使用单调时钟；不同工具的瞬时取样时刻/平均窗口不一定相同。

Docker配置使用只读宿主`/proc`，网络数据读取`/host/proc/1/net/dev`。`networkAccounting`标识统计外部接口且排除回环和常见容器bridge/veth重复流量；它不是把所有接口的计数无条件相加。若仍存在明显偏差，需在同一时段对照实际参与网卡及MobaXterm统计范围，不能仅凭单位换算宣称原因已确认。未挂载宿主proc时页面标为运行时采样；需核查实际运行容器配置，而非只看仓库模板。复杂bond/VLAN拓扑也应核对是否存在重复统计。

## 健康与准入

Worker直接向平台MongoDB更新心跳，`heartbeat`由数据库执行`$currentDate`生成，表示平台收到并写入该次心跳的时间；节点自身时间另存`workerReportedAt`，只作诊断。Worker时钟比平台快或慢8小时，只要仍持续上报就不会因此离线；没有把失联超时放宽到8小时。API与平台数据库仍使用统一平台时间，节点的真实日志时间、其它业务租约并不在本改动中重新校时。

遥测通过Worker单调时钟计算`telemetryAgeSeconds`；API用心跳接收时刻减去该年龄生成对外的`telemetry.sampledAt`，原采样墙钟另存`workerSampledAt`供诊断。持续心跳不会刷新卡住的旧样本；超时/异常样本保持未知。必须更新所有独立Worker和API，旧版本写入的本机心跳无法事后推算接收时间，不伪造历史在线状态。

超过 30 秒无心跳为 OFFLINE；遥测超过 15 秒不再参与新鲜指标判断。CPU 80%、内存 85%、磁盘 80%、写入延迟 150 ms 起提示关注。CPU/内存达到 95%、磁盘达到 90%、写入延迟超过 200 ms，或节点隔离、地址配置不一致时为严重状态。容量满和人工禁用准入也会显示明确原因。

指标压力限制新任务，不因短时 CPU 或内存升高主动迁移、终止已有任务。采集写入失败及磁盘保护仍执行原有故障处理。健康与可准入不等同：没有新鲜遥测的旧 Worker 可以作为保守候选，缺失指标不被视为空闲。

## 最佳节点规则

先排除心跳超过 15 秒、禁止准入、已删除、已隔离、配置不一致、容量满、磁盘/写延迟/CPU/内存越过硬阈值的节点。启用 NFS 监控的任务还要求节点上报 `capabilities.coredumpNfs=true`，表示配置了 NFS 服务地址；这不是设备端已挂载成功的证明。

对候选计算成本，越低越优先：容量使用比例 30%，CPU 20%，内存 20%，磁盘 10%，写入延迟 10%，网络上下行总速率 10%。CPU、内存及网络缺失时用 0.75 的保守成本。网络按当轮候选最大速率归一，只用于候选比较，不假定物理链路带宽或宣称链路已饱和。同成本按稳定节点 ID 排序。

每个新任务分配前重新获取候选指标，成功领取后更新本轮占用；MongoDB 领取事务复核节点压力、资格和任务意图，原有集群容量、运行锁和调度租约保持有效。只分配待运行任务，不为均衡迁移正在采集的任务。

## 验证边界

单元和集成测试覆盖指标未知/过期、CPU/网络差分、压力准入和多候选分配。页面模拟覆盖桌面与手机，不构成真实多台生产服务器或 500 路全天负载验收。目标服务器需更新 Worker 与 API，旧 Worker 不会上报新增指标。
