# 当前状态总表

| 当前请求记录问题 | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 请求对象统一显示未记录 | `common/request_targets.py`、`observability.py`、`administration/event_presenter.py`、前端审计表格和详情 | 保存路由声明的对象ID并批量补名称；集合/平台操作明确范围。对象及请求专项21项、前端164项、构建、真实Cookie三类审计页面及1440/390截图通过；后端全量1500项通过 | 旧记录未保存的实体ID无法恢复，集合类型可在读取时补齐；30天TTL和请求频率不变，见[当前证据](history/2026-09-15-request-target-and-client-ip.md) |
| 公司来源IP显示Docker容器地址 | 已确认用户实际运行独立frontend/backend/worker/database四个Compose项目；`deploy/config/backend.env.example`及部署说明补齐代理配置，`tests/test_proxy_headers.py`覆盖策略和请求审计 | 用户实测API的`FORWARDED_ALLOW_IPS=127.0.0.1`，不信任实际对端172.21.0.2；可信代理恢复浏览器10.41.203.12、非可信XFF仍拒绝的联合回归通过 | 公司需在实际`.env.backend`加入代理172.21.0.2后仅重建API，并通过新请求验证；未远程执行，不能宣称目标已恢复。IP白名单仍限定浏览器而非设备地址，历史误记IP不可可靠回填 |

| 当前问题 | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 跨机 Worker 在登记页在线、节点看板离线 | `administration/settings.py`、`node/health.py`统一30秒窗口并区分未来心跳；`commands/api.py`返回`assessedAt`；`nodeDashboard.ts`、`useWorkspaceCollections.ts`以服务器时间与浏览器单调计时更新快照 | 22项节点/设置回归、前端162项、生产构建、1440/390看板截图及浏览器快8小时验证通过；隔离真实API/Worker/WebSocket/小时下载通过并清理；见[心跳一致性证据](history/2026-09-15-node-heartbeat-consistency.md) | 公司目标机器的实际心跳和时钟尚未提供，不能认定其根因已证实；更新API/前端后核对`heartbeat`与`assessedAt`及A/B服务器UTC时间，真实时钟偏差须同步时间，不能放宽租约或伪造在线 |

CI恢复证据：修复提交`ae9c671`已推送，其[CI34958663525](https://github.com/geekingdom/camera-log-record-server/actions/runs/34958663525)的`cross-worker-read`作业104346735930已成功完成，覆盖双Worker补读、SSH暂停跨Worker恢复及本次失败的SIGTERM恢复步骤。记录时其它部署作业仍在运行，整条流水线结论须另核；不再将本项列为待修复。

CI修复（2026-09-15）：最新CI34956216992仅cross-worker-read失败（其余七项成功），原因是SIGTERM脚本强求瞬时STOPPED，而正常调度已转PENDING。正式调度回归先复现后修复；仅修改验证器接受两种无归属RUNNING状态，并增加旧运行结束/端点锁释放检查，39项及真实回环SIGTERM恢复通过，测试数据已清理。见[CI竞态证据](history/2026-09-15-ci-sigterm-observation.md)。当前查询已确认d051785、d78874c的CI成功，下面旧的计费阻塞仅为历史观察，不能作为当前失败原因。

部署复核（2026-09-15）：Docker Desktop29.4.0已启动，构建上下文5必要项/19排除场景通过。正式Worker镜像构建在Docker Hub基础镜像元数据阶段超时，宿主机直连仓库443亦超时，尚未进入pip或工程安装；未产出临时镜像或采集日志。原生Linux组件验证器不在macOS绕过平台检查。详见[容器构建阻塞证据](history/2026-09-15-local-container-build-check.md)。下一步恢复可信基础镜像访问后重跑；目标Linux、公司解密接口及CI计费条件仍需外部环境。

分卷组合验证（2026-09-15）：同一500路核心验证延长至40秒，各1200行/秒，共2400万行、6.144GB源字节；40.000秒提交，含排空压缩51.918秒。每路两卷，共1000个日志成员合入500个小时包，逐路摘要及前缀全部通过；归档后单独`.log`原卷为0，临时目录已删除。详见[分卷补充证据](history/2026-09-15-500-route-core-validation.md)。此次收尾归档不等于自然整点轮转，仍不证明文件可读P99、真实协议或全天混合负载。

500路本机短时证据（2026-09-15，基线`d051785`）：正式Collector/HourlyWriter接有界模拟源，500路各1200行/秒×10秒，共600万行；实际发送10.001秒，含排空压缩13.389秒，500路源摘要及前缀/包内仅日志校验全部通过。最大RSS约256MiB，临时正文及归档已删除；验证器/存储/前缀26项通过。详见[500路核心短时验证](history/2026-09-15-500-route-core-validation.md)。未测文件可读P99、真实协议、多节点及24小时混合负载，不能据此标为生产容量达标。`d051785`的[CI34955143375](https://github.com/geekingdom/camera-log-record-server/actions/runs/34955143375)已终止且无执行步骤，检查注释仍为GitHub账号付款/支出额度限制；管理员处理Billing后需重跑。

生命周期增量（2026-09-15）：API启动前段失败现在关闭已创建Mongo客户端，Mongo关闭异常仍执行日志监听器排空；新增3项先失败后通过，相关33项及API修复后全量1480项通过。Worker退出已复现并修复“已有release等待阻止其他active开始关闭”，已有收尾与其他连接并行且不重复stop、不释放未知归属，统一取消管理；Worker专项101项、最终集成42项及Ruff通过。最终真实回环SSH/SIGTERM替换Worker自动恢复、两会话初始化顺序/各12行源摘要/连接及名额释放通过，随机库和临时日志已清理；未操作实体设备。详情见[API生命周期](history/2026-09-15-api-lifespan-cleanup.md)与[Worker并行收尾](history/2026-09-15-worker-parallel-shutdown.md)。物理断电、永久阻塞落盘和多节点非合作接管仍待目标验收；目标API/Worker需更新后验收，当前本机常驻进程未为本轮重启。

本轮提交`5c371a8`已推送。其[CI34953810283](https://github.com/geekingdom/camera-log-record-server/actions/runs/34953810283)未启动任何测试步骤；GitHub检查注释明确为账号付款失败或支出上限不足。下一步由仓库管理员处理Billing后重跑该提交CI；不能把未启动标为代码测试失败或验证通过，本机验证证据见下文。

当前增量（2026-09-15，已完成本机验证）：公司PSH解密诊断、实时手动命令失败悬浮通知、运行JSONL上海时区及独立分钟历史检索。PSH分阶段记录脱敏调用/返回/异常并关联真实会话；JSONL显式上海时间带`+08:00`，三种宿主TZ隔离回归通过，本机API实际新记录已核验。后端全量1474项及后续最终专项61项、前端161项、生产构建、Ruff通过；20项生产浏览器分段全部通过，通知1440/390/320截图已检查，10秒消失（悬停暂停）/手动关闭/轮询去重/任务切换清理通过。独立分钟范围在洛杉矶浏览器时区验证上海参数及日期切换不重置；真实隔离API/Worker/WebSocket/小时下载冒烟通过，临时库和日志已清理。详见[本轮证据](history/2026-09-15-debug-diagnostics-and-log-workspace.md)。公司真实失败原因仍需目标Worker升级后采集诊断，当前版本CI须按新提交核验。

前一功能提交`a8cd376`的[CI34940029956](https://github.com/geekingdom/camera-log-record-server/actions/runs/34940029956)已全部成功，包含前后端、中文提交、完整容器、独立组件、Ubuntu原生部署、NFS及跨Worker读取。该证据不替代目标公司环境验证。

最新增量（2026-09-15）：管理员可在节点登记和编辑界面保存逐节点 `writeLatencyLimitMs`，范围为1至60000毫秒，历史节点缺省兼容200毫秒。调度、领取事务、Worker准入、健康告警和排序均采用保存值；达到75%预警，仅超过上限才拒绝新会话，不因延迟停止已有采集。真实Mongo验证500→200ms收紧竞争、恢复500ms后领取、独立节点阈值与无孤立run/lock；本机正式PATCH及实际心跳验证200→500→200热更新。后端全量1464项、前端158项、构建、20项生产浏览器、隔离真实Worker/WebSocket/小时下载冒烟、Ruff通过，临时库和日志已清理。详见[阈值验证](history/2026-09-15-node-write-latency-limit.md)。该配置不提高实际吞吐，不改变文件可读P99≤200ms的原始验收目标；目标生产容量仍待验证。

实机增量（2026-09-15）：35主机SSH在随机本机数据库、独立API/Worker及临时日志根完成两轮正常暂停/恢复。每轮暂停前后名额/FD归零，等待12秒不恢复连接；继续保持run并新建session，最终stop释放连接。3会话归档含3个日志成员，共2,022,642字节；随机库与临时日志全部回收，独立检查无35:22残留连接。见[35生命周期证据](history/2026-09-15-device35-lifecycle.md)。不将正常恢复等同重启/跨节点，也不以归档非空代替源端逐字节完整性验收。

当前版本验收：`282f982`的[CI34936992249](https://github.com/geekingdom/camera-log-record-server/actions/runs/34936992249)八项全部通过。本机生产构建20项浏览器脚本通过，检查1440/390/320像素工作台及1920/1024像素资源筛选，桌面/移动端终端和趋势图截图已查看；覆盖导航、操作权限、ANSI/级别着色、查找定位、命令历史、小时归档及缺口补读。`verify_isolated_browser_smoke.py`调用正式浏览器冒烟脚本，真实API/Worker/WebSocket/小时下载全链路通过，随机数据库与临时日志目录已删除，未访问实体设备。页面模拟验证与真实协议链路验证分别记录，不将其等同于目标服务器全天容量验收。

模块核验（2026-09-15）：`collection/runtime.py`中的调试事件记录已移入既有`runtime_events.py:record_debug_event`，会话编排降至498行；确认去重、写库失败后重试及owner保护保持原语义，47项定向测试及独立代码复审通过。该拆分后全量后端1458项、前端156项、生产构建、Ruff和差异检查通过。独立审查资源、任务、模板、账户、节点配置、命令和导出等用户持久变更路径，未发现缺少操作者业务审计的可复现问题；请求记录仅是补充证据，不替代业务事务审计。没有新增集合、连接或设备操作，生产故障及长期容量边界仍按下方需求表保留。

最新核验：功能提交 `5ebe243` 已推送，其 [CI34935808418](https://github.com/geekingdom/camera-log-record-server/actions/runs/34935808418) 八项全部成功，包含 Linux 标准80端口真实Worker SIGTERM恢复、完整Docker重复部署、独立组件、原生部署、NFS、前后端及中文提交检查。随后针对持续目标复核连接回收、协议保活、十秒无正文超时、SSH/Telnet设备暂停恢复、跨包行前缀及小时命名，相关49项测试通过。测试使用本机协议源及临时目录，没有连接或重启34/35实体设备；这不替代物理多节点、长期背压与生产容量验收。下段“CI须按对应SHA另核”的待核事项已由本条完成。

最新业务增量（2026-09-15）：容器重部署恢复、批量写入和生产PSH配置已实现。Worker退出保留用户运行/暂停/停止意图，Docker收尾宽限120秒；撤销准入失败只隔离、关闭失败不重试，16路有界并发收尾。真实SIGTERM替换Worker自动新run/session、两会话各12行摘要与源一致并清理；参见[重部署验证](history/2026-09-15-worker-redeployment.md)。新存储四项故障测试、相关35项、32路×1200行/秒×3秒共115200行逐路归档摘要通过，实验目录已清理；120小包×60批本机对比8.121ms→0.545ms且1750206字节摘要一致，参见[写入验证](history/2026-09-15-write-batching.md)。PSH协议与参考脚本核对、Docker/原生环境生成和101项相关测试通过，真实服务调用仍待公司网络；配置见[生产PSH](psh-production.md)。关闭/存储最终相关63项、固定源码后端全量1458项和Ruff通过；本机API/Worker已受控重载，健康200、local-dev准入正常、无活动任务与实验数据库残留。上一提交47e0852的CI34920951879已成功，本次提交CI须按对应SHA另核。

核对日期：2026-09-15。本轮四项功能已提交并推送为 `4953970`：输入速率准入、增长记录维护、跨文件/节点缺口补读、故障导出产物回收。该功能提交的CI34917184543七项全部成功；目标环境、500路全天及物理多节点仍待验收。完整本轮证据见[四项功能改进验证记录](history/2026-09-15-four-gaps-validation.md)，此前阶段证据已迁至[四项功能前的历史快照](history/2026-09-15-before-four-gaps.md)。R81比率图以首个有效值为100%；R79主从SSH与R80认证退避仍保留各自未完成的实机/生产验收。恢复上下文先核对最新业务消息、此表、工作区和最新提交，摘要只作为证据线索。

最新推进：用户确认暂时没有目标Linux服务器，先完善本机可验证部分。`4c23594`的CI34918920031八项成功。`scripts/verify_cross_worker_gap.py`同机双Worker真实补读验证已通过：A/B/A三段各70KiB，非零锚点、4KiB续读与215004字节摘要一致；B真实停止后503而A仍200；随机库及本轮文件已回收。该验证不代表采集连接迁移或物理网络分区。详见[双Worker补读证据](history/2026-09-15-cross-worker-gap.md)。

前一增量：调度候选和领取事务采用管理员保存的节点容量/准入，六项回归、真实Mongo竞争通过，详见[保存节点准入证据](history/2026-09-15-saved-node-admission.md)。真实SSH双Worker暂停恢复已通过：A关闭连接、重新认证、B同run新session、初始化重放、定时预算累计两次、两节点各26行正文比对与清理；见[真实SSH恢复证据](history/2026-09-15-cross-worker-ssh-resume.md)。47e0852的Linux CI八项成功，含标准80端口验证，不再列为待验。

| 本轮优先项 | 当前实现位置 | 已核验证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 输入速率和写入延迟硬准入 | `node/input_admission.py`、`node/write_pressure.py`、调度/领取/Worker、后台节点配置 | 输入速率真实Mongo配置竞争、Worker准入、前端保存及定向验证通过；写入延迟逐节点1至60000ms，75%预警、超限仅拒绝新会话，真实Mongo验证放宽/收紧竞争、独立节点阈值及无孤立run/lock；本机重载后`local-dev`新心跳为`inputBytesPerSecond=0` | 目标多节点部署、真实持续吞吐及按目标硬件标定阈值 |
| 增长记录治理 | `common/record_archive.py`、`record_purge.py`、`record_maintenance.py`、`retention_config.py`、设置API及组件 | 真实Mongo验证摘要故障回滚、1001条分段、租约互斥/恢复；五类Explain均10结果/10键/10文档且无SORT；本机重载后维护租约行已出现 | 生产数据规模索引/复制成本及长期维护验收 |
| 实时缺口跨文件/节点补读 | `logs/gap_catalog.py`、`gap_snapshots.py`、`LiveLogRanges.vue` | 目录锚点、短游标、24小时/500文件/200片段边界，权限/分页/字节边界/迟到响应和20项生产浏览器验证通过；本机归档/清理/缺口/设置定向24项通过 | 物理跨节点与真实持续采集验收 |
| 异常下载产物回收 | `logs/export_locks.py`、`export_readers.py`、`export_writer.py`、`jobs.py`、`maintenance.py`、节点下载端点 | 写入关闭证据、读者租约、CAS、`flock`、取消/失联恢复和定向验证通过；受控重载前无活动作业、任务归属或端点锁 | 真实大文件断电/多人持续下载和目标部署验收；未知归属旧目录保守保留 |

主代理负责接口衔接、审核、验证和总表，子代理承担独立实现、回归及文档核对。本轮没有连接实体设备。此前运行快照不代表当前服务状态，部署需保持前端/API/Worker同版。按上海日期展示归档及检索；显示层着色/查找不改原始采集字节。完整平台仍有下列生产验收事项，不能宣称全部完成。

R46既有实机验收已通过，证据保留在[实机暂停重启记录](history/2026-09-11-ssh-paused-reboot.md)及下方需求表。本轮不重复列为开发缺口，也不将该历史结果当作当前设备状态。

## 当前实现与验证

- `4953970`已提交并推送。后端全量1433项、前端156项、四项功能相关定向24项及导出相关定向53项通过；20项生产构建页面验证通过。该功能提交的[CI34917184543](https://github.com/geekingdom/camera-log-record-server/actions/runs/34917184543)七项全部成功；不替代目标部署验收。
- 真实Mongo验证覆盖速率配置竞争、增长记录摘要故障回滚、1001条分段、维护租约互斥/恢复及五类Explain；每类均为10结果/10键/10文档且无阻塞SORT。随机库在`finally`删除。
- 实时缺口支持跨文件/跨会话锚点、短游标及24小时/500文件/200片段边界；中间`DELETED`文件按`FILE_UNAVAILABLE`持久呈现，不混入新目录。异常导出回收仅处理具备关闭和归属证据的终态作业，未知旧目录继续保留。
- 受控重载前无`RUNNING`/`PAUSED`任务、活动作业、任务归属和端点锁；新API/Worker、`8000`/`8001`/`5173`健康检查和`local-dev`心跳均已核验，维护租约行已出现。此为本机加载状态，不替代生产验收。
- 迁出的历史阶段证据、CI和实机记录见[四项功能前的历史快照](history/2026-09-15-before-four-gaps.md)；各需求的最终实现位置、验证与剩余风险仍以下方R表为准。

## 未完成与下一步

当前阶段：主要业务功能已实现，处于可靠性加固和生产验收阶段，可供开发/受控试用，尚不能宣称500路生产目标已达成。不按测试数量或需求行数推算完成百分比。功能缺口、验收缺口和目标部署待更新应分开管理。

| 优先级与性质 | 当前真实差距 | 下一步及完成证据 |
| --- | --- | --- |
| P1 验收：节点输入速率准入 | 准入阈值、调度/领取/Worker复核、后台保存和真实Mongo配置竞争已完成 | 目标多节点持续吞吐与阈值标定；不把50 MiB/s默认值当作容量承诺 |
| P1 验收：增长记录治理 | `recordRetention`、租约栅栏、引用保护、分段归档、审计摘要及真实Mongo竞争/回滚/Explain验证已完成 | 生产数据分布、索引空间、复制延迟和长期维护观测；不按终态直接TTL |
| P1 验收：容量与大文件 | 500路各1200行/秒连续24小时、原口径文件可读P99、持续压缩及多人下载混合负载尚无等规模证据 | 目标Linux集群逐路源序号和摘要比对、独立文件探针、真实压缩率及磁盘预算报告 |
| P1 验收：多节点故障 | 同机双真实Worker已验证SSH合作暂停、旧连接释放、认证后换节点恢复原运行与定时预算、实际源日志比对；物理跨机网络分区、旧Worker隔离及接管未完成 | 有目标Linux服务器后验证A完整平台+B独立Worker、断网/重启/磁盘失败/接管矩阵；不重复把合作暂停恢复列为未实现 |
| P2 验收：实时缺口定位 | 跨文件/跨会话目录定位、短游标及前端顺序补读已实现；同机双真实Worker目录分页、非零锚点与字节摘要、停止单节点故障隔离验证已通过，4c23594的Linux CI八项成功 | 物理跨节点和真实持续采集；没有被服务端保存的源端正文无法补造 |
| P2 验收：异常产物回收 | 输出归属、关闭证据、读者租约、CAS与文件锁互斥、取消/失联恢复已完成 | 真实大文件中断/重启/并发下载；未知归属或无关闭证据的旧目录继续保留，不能宣称全自动清除 |
| P2 验收：设备差异与外部接口 | 从机2/3、真实PSH解密服务、10003串口切换、Telnet NFS、真实换机/改密和长时认证退避未完整覆盖 | 具备设备/接口条件后专项验证；保留已完成35主从重启、SSH NFS卸载/重挂及CPU监控证据 |
| P2 验收：数据库与部署 | 生产数据规模索引占用/复制延迟、目标HTTPS代理、物理宿主机重启自启动及发行版差异未验收 | 使用目标拓扑及真实数据分布验证；不以CI绿灯代替目标服务器验收 |

管理事件游标分页、按需总数、预算与执行记录事务、SSH NFS实机lazy卸载、默认监控规则和前端导航已完成，不重新列为功能待实现。目标服务器是否升级需独立核对版本，不能从本机运行或仓库提交推断。

本轮前的过程记录已移至[状态历史快照](history/2026-09-11-before-authentication-cursor.md)。其中旧的“下一步”、进程状态和测试数量仅为历史，不能覆盖本表。每项最终需求、实现位置、验证和剩余工作以下表为准。

## 需求对照

后端简写路径均相对 `backend/camera_logs/`，前端组件位于 `frontend/src/features/`。局部验证通过不等于整体交付。

| ID / 最终需求 | 实现位置与状态 | 验证证据 | 未完成部分 | 下一步 |
| --- | --- | --- | --- | --- |
| R88 PSH分阶段调用/返回/异常诊断、手动debug错误悬浮通知、运行日志北京时间 | `collection/psh_passwords.py`、`psh_diagnostics.py`、`psh_dialogue.py`、runtime事件；LiveLogs顶部通知10秒自动关闭/可手动关闭；`common/observability.py` | 三种宿主TZ、HTTP状态/非JSON/配置/mock/密钥回显/事件库失效诊断通过；全量1474项及最终专项61项；生产通知浏览器1440/390/320验证关闭、长文滚动、去重及任务切换 | 公司真实失败原因尚未知；旧JSONL不会重写，通知悬停暂停倒计时 | 更新前端/API/Worker；按task/run/session查JSONL诊断，不能补造历史返回 |
| R89 历史检索独立自定义日期时间至分钟 | `LogArchives.vue`、`archiveDate.ts`；复用后端带时区`start/end`和流式接收索引 | 前端161项；同小时四行按08:15至08:17仅命中两行；生产预览浏览器在洛杉矶时区验证上海时间转UTC、归档日期切换不重置检索及1440/390布局，均通过 | 单次最长24小时；按接收索引时间非设备正文时间；结束边界不包含 | 更新目标前端即可使用独立范围；后端既有时间契约保持不变 |
| R87 前端逐节点配置写入延迟上限并热生效 | `administration/settings.py`、`node/write_pressure.py`、health/worker、scheduler/claim；SettingsManager登记/编辑及NodeList展示；站内API说明已同步 | 全量后端1464、前端158、真实Mongo事务竞争、独立节点、正式PATCH及心跳200→500→200；20项生产页面、1440/390配置截图及隔离真实冒烟通过 | 无本项已知开发缺口；Worker下次心跳生效，目标环境需升级同版 | 部署前端/API/Worker；管理员按目标硬件标定各节点，保留全天容量验收 |
| R84 设备型号和序列号模糊查询、资源工具栏不重叠 | `ResourceWorkspace.vue`身份输入与自适应网格/换行操作区；`shared/api.ts`显式类型；复用`resources/api.py`字面子串筛选 | 资源发现10项、前端API参数测试；`browser_resource_identity_filters.mjs`组合条件、清除、分页/选择复位、五视口和桌面双侧栏状态；20项生产浏览器通过，截图已检查 | 目标部署需更新前端 | 更新静态产物并刷新页面 |
| R85 趋势弹窗构建大块警告 | `frontend/vite.config.ts`按echarts/zrender库边界分块，保留弹窗按需加载 | 生产构建：弹窗13.78KB、echarts374.08KB、zrender176.99KB；没有提高500KB阈值；趋势浏览器canvas/切换通过 | 分块不表示总下载量降低；未进行生产网络加载耗时验收 | 保持按需加载回归，目标部署更新静态产物 |
| R86 独立部署失败输出可诊断 | `scripts/component_diagnostics.py`仅展示固定分类/提示，不回显第三方原文；`verify_component_deployment.py`接入非零退出路径 | 相关37项通过，覆盖外部Basic、含空格密码、编码值、自由文本凭据；39d3595的CI34843428794独立组件及其它六项成功 | 首次历史故障根因未知；未扩展超时路径 | 保持固定分类输出，具体根因在部署主机核对 |
| R83 管理事件游标分页和按需总数 | `administration/event_cursor.py`及query/API；前端audit分页；站内API目录；已实现 | 后端1375/前端154/构建；真实Mongo混合ID同时间10页无重漏、派生/legacy/422；第5页26键/26文档；真实Cookie浏览器1440/390；本机API已加载；提交749cc6f的CI34837902270成功 | 低选择性过滤和旧时间兼容扫描不能由游标消除；生产长期容量尚未验收 | 按真实目标数据测低选择性过滤成本 |
| R82 审计来源与例行监控事件降噪 | `administration/event_sources.py`及presenter/query；WS上下文；`collection/runtime.py`和coredump记录；前端audit来源列及详情 | 后端1370/前端149/构建；ASH/NFS会话去重、故障恢复、写库失败重试、WS服务账号上下文；真实Mongo派生筛选、Cookie三事件及Worker/WS/下载浏览器；1440/390截图通过；API/Worker已加载 | 历史缺失身份不能可靠补造；旧重复记录保留；同样的真实失败仍逐次记录；目标部署尚待更新 | 核对本次提交CI并在目标环境更新API/Worker/前端 |
| R81 同名进程统一对象与内存比率（首个有效值100%） | `resource_metrics/runtime.py`按name保存process指标；`resourceMetrics.ts`历史合并及CSV；`metricRelative.ts`和趋势弹窗新增模式 | 后端1362项、前端146项；隔离browser_smoke、1440/390趋势检查通过；测试覆盖PID变化、同名累计、缺口、零基准、身份图例；Worker已加载 | 尚未用设备重启产生新PID再实测；真实长期监控验收沿用R76边界 | 继续目标环境长期验收 |
| R80 认证失败分级退避与资源行立即认证 | `resources/health.py`持久计数/下一探测时间；`resources/api.py`空body需任务控制权限并在事务内重核关联任务归属、完整body编辑预览；ResourceWorkspace行操作/确认/失败刷新；API目录及[策略](resource-authentication-policy.md) | 集成1345项；真实Mongo覆盖9/10/21/22/45/46边界、身份更新、清零、失败不推进、CAS与回滚；越权403无写入；1440/390页面及19生产脚本；已加载本机 | 未实际等待数天验证自然定时，物理多API竞争仍需目标环境验收 | 目标部署持续运行验证；显式resume不受长退避阻挡 |
| R79 主机/从机SSH任务、资源共享扩展端口与重启恢复 | 模型及任务表单sshTarget；`collection/slave_shell.py`、`slave_ssh.py`、`slave_events.py`及内部固定引导接口；每IP+端口最多5连接；超时/取消未知保留引导租约、不临时重发 | 集成1345项、前端139项/构建；真实Mongo跨进程配额；真实Collector取消FIFO回归；结构化事件覆盖固定关联字段、未知/取消、服务未就绪和写入失败隔离；35主机22+两从机18080，暂停恢复及600秒窗口内530.41秒重启恢复，ifconfig归档验证来源，停止/清理完成；已加载本机 | 从机2/3、真实PSH及物理跨Worker未实测；扩展服务发送已开始时不可撤销，按未知处理 | 目标多Worker及多型号验证；不重复已完成35重启实验 |
| R78 可选监控装配失败不重连日志、会话模块职责收敛 | `collection/runtime_monitors.py`装配、等待来源和收尾；runtime复用监督协程，修复已提交`a4d76d3` | 48项定向回归；2026-09-14核实CI34585838738及056515d的CI34586054230成功；此前受控重载健康200 | 目标部署待验证；09-11进程状态不代表当前运行 | 继续目标部署验证；本机健康不代替设备故障注入或规模验收 |
| R77 通用/专用节点、多个资源IP/CIDR规则及专用优先分配 | `node/resource_routing.py`、scheduler/claim、节点配置模型及SettingsManager；已实现 | 节点/平台/调度26项；1440/390配置浏览器；35两任务在通用节点同时在线时均分配专用节点；认领事务重新核验规则 | 现有运行不强制迁移，物理多机故障压力验收未完成 | 目标Linux多机验证专用容量耗尽后的通用回退 |
| R76 资源级CPU与内存监控、默认规则、逐项失败隔离及单图动态纵轴 | `resource_metrics/`、单接收器旁路捕获、settings.resourceMonitor、指标弹窗/metricAxis及规则编辑器；已实现，见[资源监控](resource-monitoring.md) | 35实测7指标，暂停不增长、恢复采样；首指标/PID/规则失败后继续及下一轮恢复；真实Mongo2键/2文档无SORT；136前端及17生产浏览器，单图/单位/非零纵轴与20万点计算通过；构建分块提示已由R85处理 | 真实Mongo仅小样本；长保留/多型号Telnet实体采样、规模性能未验收 | 按目标资源数测90天桶大小与复制延迟，继续物理多机验收；不重复开发默认规则 |
| R75 Coredump从任务开关迁移到资源、最后活动任务退出才卸载 | 资源模型/API、任务表单和绑定、coredump租约/接管/动态开关；已实现 | 88项定向；35两SSH交接、最后暂停UNMOUNTED及恢复MOUNTED，BusyBox实际挂载点卸载先红后绿；实验清理完成 | 跨节点target/source与禁用竞态有模拟回归，尚未物理跨节点/Telnet实机；设备离线不能保证umount送达 | 在目标Linux多机完成认证失败、源节点故障及Telnet设备矩阵 |
| R74 工作台交互纳入每次提交的生产页面回归 | `frontend/scripts/verify-workspace.mjs`及CI前端步骤含实时终端、归档阅读器、缺口补读、主从表单、手动认证和资源身份筛选，现共20脚本 | 本机20项通过；39d3595的CI34843428794前端成功 | 目标服务器仍需部署相同构建 | 保持CI布局断言并验收目标部署 |
| R73 专业终端工作台、着色、命令历史及实时查找；当天小时归档、完整行检索与上下文分页 | `LiveLogs.vue`、共享LogText/FindBar与history、`LogsWorkspace.vue`、`LogArchives.vue`、`LogFileViewer.vue`、search_stream/jobs和安全文件元数据API；已实现并加载本机 | 后端1258项、前端127项/构建、Ruff/diff、CodeGraph；1440/390/320终端与布局/补读、1440/390归档浏览器及截图；隔离真实Worker/WS/小时下载通过并清理；独立审查结束时间边界已先红后绿 | 专业日志查看并非完整交互终端仿真；实时最多1000匹配导航且有上限提示，段内查找仅当前段；单行256KiB/结果8MiB明确限制；目标生产环境未部署 | 部署前端/API/Worker同版，在目标环境验收；保留R20全天性能，R46实机重启已完成 |
| R72 运行事件时间规范化、默认索引及兼容旧库 | `common/runtime_event_time.py`、回填CLI、`event_queries.py`及`event_indexes.py`；已实现并加载本机 | 后端1239项、补强回填4项、Ruff/diff；真实Mongo两万条默认/任务/节点/类型50键/文档且无SORT，迁移前后等价；本机回填2条、剩余0、事件索引6个及API健康通过 | 旧库需低峰回填；旧格式写入端混跑不保证探测与查询间快照；派生筛选及count/skip仍有成本 | 更新全部写入端后预览/分批回填；继续管理页游标和生产成本验收 |
| R71 认证记录页面使用游标避免深页扫描及总数统计 | `AuthenticationRecordsDialog.vue`、`authenticationRecordPager.ts`及共享API；显式空游标首屏，失败重试和请求代次隔离 | 后端1234项、前端115项/构建、认证浏览器1440/390及独立复审；隔离真实Worker/WebSocket/小时下载通过，临时库与日志已清理 | 旧页码API仍有count/skip成本；游标不是快照，记录新增/到期会改变可见页面 | 生产查询容量验证；运行事件时间规范化已完成见R72，勿重复实施 |
| R70 Docker及原生部署整数环境配置可正常启动 | `common/config.py`前置解析六个严格整数字段；节点验收读取Compose实际配置 | 环境及dotenv先红后绿；新增50项，全量1227项；CI34569446838七项全绿，完整部署/重复部署/节点登记/代理采集下载通过 | 目标物理服务器网络及全天负载未验收 | 继续目标环境验证，不重复修复已通过的启动与节点验收 |
| R69 手动设备认证历史与操作审计原子保存 | `resources/api.py`复用`common/audited_mutations.py`；已实现且API已加载 | 后端1177、前端107/构建；真实Mongo六种故障注入、临时库/文件清理、独立审核及本机健康通过 | 提交结果未知时返回503，客户端跨请求重发没有专门去重；完整故障矩阵仍未验收 | 保持设备访问在事务外，按生产故障矩阵验证审计可追溯性 |
| R68 Collector命令派发模块化及命令等待隔离 | `collection/command_dispatcher.py`与collector；已实现并加载本机 | 后端1175、前端107/构建、六项新边界、旧版本取消对照、32路合成完整性、三服务200及心跳0.035秒 | 预算回调已开始后取消不退回持久预留；本次未重跑实体设备，多机全天验收未完成 | 目标部署验证命令、日志和NFS同会话链路；保持明确的发送不确定性边界 |
| R67 异常可追踪与Worker遥测模块化 | `common/observability.py`、main、`node/telemetry_runtime.py`、worker、connections；API异常启动/关闭清理已补齐 | 既有遥测超时、真实FastAPI异常链路及脱敏回归；新增API生命周期3项先失败后通过，相关33项及API修复后全量1480项通过 | 未审查所有第三方日志输出；生产故障矩阵未验收；Collector拆分已在R68完成 | 更新目标API后验证重部署，不能以调用关闭入口等同断电排空 |
| R66 空闲超时与读取失败可追踪、运行模块拆分 | `collection/runtime_events.py`、runtime、事件展示/筛选、SSH验证器；已实现并加载本机 | 全量1144、真实Mongo派生筛选、35旧session对应IDLE_TIMEOUT及同run新session实测、最终FD/名额0和日志清理 | 事件写入尽力且有1秒上限，数据库异常时可能仅有服务日志；整个Mongo不可用不是本轮保障；运行事件长期治理已由R64受保护维护器接管，生产长期成本仍未验收 | 目标环境加载并按事件task/run/session排障，结合增长预算观察维护吞吐、索引和复制延迟 |
| R65 SSH跨Worker每IP+端口最多五连接、关闭确认后释放 | `collection/ssh_admission.py`、connections/runtime、Worker入口和BLOCKED恢复；按09-14主从需求改为端口独立名额，22兼容旧IP键 | 70项配额/生命周期相关回归；真实Mongo跨进程18080满额、18081独立、按运行跨端点精确释放通过；35主机22+两从机18080最终claims=0 | 新旧Worker混跑不受完整限制，真实Linux跨机故障隔离未验收；平台外SSH不可计入控制 | 部署先收尾旧会话并升级全部Worker，完成目标网络故障矩阵验证 |
| R64 数据库增长治理、认证90天保留和连续成功压缩 | `authentication_records.py`事务head/日聚合，资源API/弹窗，`event_indexes.py`、`record_archive.py`、`record_purge.py`、`record_maintenance.py`、TTL回填脚本及`database-retention.md`；已实现且本机安装 | 认证旧索引双初始化和33次并发认证验证；增长记录真实Mongo验证摘要故障回滚、1001条分段、租约互斥/恢复；`runs`、`commands`、`idempotency`、`operations`、`jobs`均10结果/10键/10文档且无SORT | 生产大库索引空间/写入成本、未迁移旧库兼容排序及长期维护效果未验收 | 目标部署安装索引、低峰回填旧认证TTL；以真实数据分布复核Explain、索引大小、复制延迟与受保护候选，不重复规划已完成的归档/清理实现 |
| R63 登录默认资源页、刷新保留原页 | `app/App.vue`hash导航与会话权限回退，已实现 | `browser_default_resource_navigation.mjs`及12项生产浏览器联合通过；1440/390截图 | 目标静态页面待部署 | 更新前端后刷新一次加载新版本 |
| R62 A完整平台/B独立Worker跨主机部署，自定义Mongo密码 | `deploy-all.sh`、`deploy-worker.sh`、`scripts/deploy_cluster.py`内部预检、基础Compose默认认证及原生公告地址迁移，已实现 | 部署57项；字面密码、三成员认证与错误key拒绝；原生43项；CI34479515360真实Linux完整/重复/独立组件/原生部署通过 | 独立Linux双机网络、防火墙与持续运行未验收 | 按部署文档在A/B配置可达IP、同库名及共享凭据后验收 |
| R61 新任务按最佳Worker动态分配 | `node/health.py`、`tasks/scheduler.py`、`tasks/claim.py`；保存容量/准入直接参与候选及事务复核，已加载本机API | 六项保存配置回归、真实Mongo并发关闭/收紧/放宽验证；本轮真实Mongo 500任务/8节点3966.983ms、每节点最多63；见保存节点准入证据 | 真实跨机持续混合负载未验收；打开准入仍需有效健康心跳 | 目标集群验证；不将调度500个任务等同500路持续采集 |
| R60 节点CPU/内存/上下行/健康看板及稳定显示 | `node/telemetry.py`、`node/worker.py`、`commands/api.py`、`nodes/NodeList.vue`、Docker只读proc，已实现并加载本机服务 | 61项相邻回归；1440/390模拟及真实节点API看板；清理前两节点、清理后local-dev均HOST/OK且指标非空；驻留刷新顺序稳定 | 目标Linux指标范围待验收；Docker Desktop展示Linux虚拟机指标 | 更新目标API/Worker和前端后核对指标 |
| R59 Coredump默认5秒扫描 | `common/config.py:coredump_scan_interval_seconds`默认5，Worker单扫描任务 | 默认值5/显式覆盖12核验；相邻扫描回归 | 5秒真实设备发现延迟未实测 | 目标Worker更新；保持10秒源稳定观察 |
| R58 海康认证记录及结果/时间/变更筛选 | `resources/authentication_records.py`、资源API/health、`AuthenticationRecordsDialog.vue`，已实现 | CREATE/EDIT/MANUAL/PERIODIC写入路径；正式endpoint成功/凭据/离线/异常；3项分页筛选回归及1440/390浏览器 | 旧未记录历史不可补造；目标部署待更新 | API加载后核对新增认证历史 |
| R57 Telnet设备支持NFS，串口不支持 | models/resource_binding/runtime/coredump_monitor/表单统一SSH与TELNET_DEVICE资格，共用资源租约 | 后端协议和共享负责人回归；桌面/手机共享监控浏览器 | 真实Telnet设备自动挂载/每分钟重挂尚未实测 | 目标设备受控联调 |
| R56 Telnet设备支持暂停恢复 | `tasks/control.py`、`taskActions.ts`，保留run/预算；Telnet串口拒绝 | 本地真实TCP/Telnet暂停EOF/恢复新会话/初始化两次/预算1到2通过；API/按钮回归 | 真实设备暂停/恢复待实测 | 部署后设备验收 |
| R55 命令记录展示正文及单项进度 | `commands/reservation.py`正文快照、`commands/history.py`分页筛选、`CommandHistory.vue`，已实现 | 相邻29项；1440/390浏览器独立命令筛选/切换任务 | 旧已删配置无法还原正文 | 更新API和页面；保持UNAVAILABLE说明 |
| R54 等待隔离可重新启动/停止、活跃数准确 | `tasks/recovery.py`、`node/recovery.py`、Worker及`BlockedRestartDialog.vue`，已实现 | 收据/错误11项、监督归属40项；真实Mongo隔离临时库STOP/restart/过代收据；1440/390浏览器 | 未知旧实例仍需真正隔离；不以心跳代替关闭证明 | 目标部署并核对旧任务收尾证据 |
| R53 NFS负责网络任务结束lazy卸载/恢复重挂 | `collection/coredump_cleanup.py`、monitor/lease/runtime，复用原连接发送umount -l并确认源消失 | 79项相关回归；35两SSH任务交接、最后暂停UNMOUNTED、恢复MOUNTED、最终停止UNMOUNTED已实测，见09-11资源监控记录 | Telnet与物理跨节点未实测；成功不证明传输结束，离线时不能保证命令送达 | 目标Telnet设备和跨节点验证，不重复列SSH实机卸载为未完成 |
| R52 第三方默认允许Coredump查询、导出及下载 | 沿用`users/sessions.py`基础logs:read/download和`common/security.py`绑定用户继承；`coredumps/api.py`正式入口；权限标签与站内指南明确包含core | `test_coredump_service_permissions.py`普通scopes空令牌：共享资源查询、本人导出、Bearer全量/Range与浏览器票据准入；他人导出、IP收窄、禁用/删除拒绝；相关28项，前端85项/构建及1440/390弹窗浏览器断言通过 | 节点内容代理替身不证明字节传输，真实传输沿用R43；目标页面文案待部署 | 更新前端及API说明，无需重建已有服务账号 |
| R49 平台密码至少8位 | `users/models.py`及账号/本人改密前端，创建/重置/修改统一8至128；部署文档同步 | 7拒绝/8接受接口与前端测试；本机OpenAPI三个模型minLength8 | 目标服务器待更新 | 更新目标API和前端 |
| R50 Coredump接收状态、时间语义、隐藏flag和子目录发现延迟 | `coredumps/scanner.py`源观测状态和单资源回绕；API过滤历史flag；CoredumpFiles自动刷新与明确时间标签 | 35首轮页面发现/导出/flag过滤通过；第二轮默认10秒扫描预先就绪，重启后新core从2,215,936增长至6,725,382字节，17:12:44稳定；全量和Range源摘要一致，详见Docker实测记录 | 稳定是观测判断非设备完成通知；kill至首见31.8秒含设备生成耗时，非纯扫描延迟；目标服务器需更新，大目录受扫描配额影响 | 目标部署验证；保留源文件，不按flag或catalog新ID推断新core |
| R51 设备离线后卡等待隔离，上线自动恢复原采集任务 | `collection/collector.py`识别AsyncSSH断线；`node/worker.py`同进程同owner收尾重试；`resources/health.py`OFFLINE恢复授权及安全消费 | 后端938项、前端85项/构建；真实Mongo恢复/STOP竞争/锁保护；35真实reboot：OFFLINE约125秒后恢复，新run/session，ONLINE约5秒后COLLECTING，专用任务收尾通过 | 目标服务器需部署；未知旧实例或真实落盘失败不盲目解除BLOCKED；旧无原因marker保守保留 | 目标API/Worker更新并核对遗留任务是否需要外部隔离 |
| R47 同资源单路采集SSH负责NFS，其它新建SSH共享只读开启并显示负责人 | `resources/api.py:coredump-monitor`状态接口；`runtime.py`资源CAS含run/generation/node；`CoredumpMonitorControl.vue`与独立轮询组件，展示不写成新任务配置，站内API已更新 | 主代理后端904项、前端78项/构建；真实Mongo竞争和隔离真实Worker/WS/小时下载通过并清理；整套生产浏览器及共享组件1440/390通过并查看截图；CI34447026933七作业全部通过；本机进程更新后5173代理状态接口200 | 真实设备/跨节点故障矩阵未验收 | 更新目标API/Worker及前端，完成真实设备验证 |
| R48 API文档接口分类横向滚动条不遮挡文字 | `ApiReferenceWorkspace.vue:.api-groups`禁止Flex收缩、稳定滚动槽并保留横向滚动 | 1440旧样式高度16px/按钮底部留白6.28px失败，新样式39px/client留白12px通过；主代理及Linux CI34447026933生产浏览器1440/390/320首尾分类可见、底部留白通过 | 目标服务器需更新前端静态文件，已打开页面需刷新 | 部署更新后保持该布局回归 |
| R42 海康 SSH 可选 coredump、自动 ASH/NFS 挂载及每分钟检查 | `collection/coredump_monitor.py`、`collector.py`、`runtime.py`，原发送队列/会话取消；同资源租约；Docker/原生宿主机NFS固定星号导出 | 35默认ASH，经正式命令队列gdbcfg成功挂载本机Docker NFSv3，mount确认/run/coredump；kill -6生成core时日志继续COLLECTING；Ubuntu v3/v4既有CI有效 | 本次手动命令实测不证明自动一分钟重挂全流程；跨节点隔离和持续大文件未验收；Mac卷/端口代理与Linux宿主部署有差异 | 目标Linux验证自动监控与挂载丢失恢复 |
| R43 coredump多设备大文件接收、查询、批量下载；日志多人稳定下载 | `coredumps/`扫描、冻结、配额、导出；读者委托、清理恢复和孤儿回收；平台/节点Range与原生票据 | 35真实core经不同Worker扫描、平台页面导出；两路完整下载和1MiB Range摘要与NFS源一致；文件名/时间查询正确；既有Ubuntu双64MiB及2GiB×2 HTTP证据保留 | 跨机超大文件、长期混合负载及全部断电窗口未验收；本次真实core约6.72MB；首次发现不是传输结束时间 | 目标Linux持续混合负载验收 |
| R44 海康资源周期认证、设备变更后更新身份及目录，失败停止采集/NFS | `resources/health.py`、资源API及调度：成功60秒/8并发，失败按R80退避；探测租约与revision；OFFLINE周期成功自动授权，AUTH_FAILED/ERROR需用户认证；换身份受控新运行 | 独立真实Mongo恢复消费、手动STOP竞争、锁保护通过；35两轮真实离线/上线自动恢复见R51，主从联动见R79 | 多API进程长期运行、真实换机与凭据更新联调未验收 | 继续目标服务器换机与凭据更新验证 |
| R45 资源编辑回填、类型/IP只读，名称/HTTP认证可编辑 | `ResourceEditor.vue`首次挂载immediate回填，类型/IP只读，空密码保留 | `browser_resource_prefill.mjs`桌面1440及手机390字段/宽度断言通过；主代理已查看390稳定截图，无超宽 | 真实设备编辑认证与任务恢复端到端未验收 | 目标设备联调 |
| R46 第三方暂停设备重启超过一分钟，离线缓存不阻止显式恢复 | `tasks/control.py`、`resources/health.py`：WAITING_DEVICE与新探测；每秒扫描到期资源，正常认证周期60秒；验收脚本补参数/资源/收尾校验 | 35正式API实机：reboot一次、PAUSED65.195秒、OFFLINE时resume→WAITING_DEVICE→同run新session采集→停止，FD及名额0；相邻工具回归35项通过；[证据](history/2026-09-11-ssh-paused-reboot.md) | 单设备本机Worker验收；资源认证为抽样，不证明设备精确离线时长；慢认证批次仍可能延迟扫描 | 保持回归，后续验证目标Linux多机环境 |
| R01 Python/Vue3/Mongo，控制台与第三方共用 API | `main.py`、前端、`pyproject.toml`，已实现 | 源码；历史 CI 34296982222 | 多节点部署未验收 | 部署复核 |
| R02 海康 Digest/Basic 认证，解析型号、短序列号、版本 | `resources/authentication.py`、`resources/api.py`，已实现 | `tests/test_resources.py` | 本轮未重测实体认证 | 401、异常、XML 分支验收 |
| R03 串口资源、多任务、协议/IP 限制，串口可选服务器或自填 | `tasks/resource_binding.py`、资源前端，已实现 | `tests/test_task_resources.py` | 全流程设备验收需补充 | 资源→任务验收 |
| R04 相同设备身份同父目录；软删除保留日志 | `tasks/resource_binding.py`、`resources/lifecycle.py`、`logs/storage.py`，已实现 | 资源/存储测试 | 跨节点仅相对路径一致，非共享物理盘 | 跨节点目录验证 |
| R05 表单、初始化排序、正整数定时参数、IME、密码保留 | `common/models.py`、`TaskEditor.vue`、`CommandEditor.vue`，已实现 | 模型/表单测试，历史浏览器冒烟 | 二次确认见 R16 | 统一交互验收 |
| R06 模板 CRUD/版本/独立副本与计数 | 模板模块、命令编辑器，已实现 | 模板/任务测试 | 目标服务器待部署；软删同创建者名称继续占用 | 目标部署后验证模板到任务流程 |
| R07 逐路隔离、有序写入、重复正文保留及行首上海时间 | `collection/collector.py`、`collection/runtime.py`、`logs/storage.py`、`collection/line_prefix.py`，部分验收 | 存储/故障测试及短时摘要报告；ANSI CSI/OSC、CRLF跨包精确字节与重复正文回归，连接/存储/归档联合43项通过 | 500 路全天未证明；SSH PTY 不证明跨独立流时序 | 独立源序号验收 |
| R08 SSH 按凭据连接、不以变化指纹拦截，各协议保活、十秒空闲重连及暂停恢复 | `collection/connections.py`、`collector.py`、`runtime.py`、Worker.pause及`node/shutdown.py`；本地真实SSH和设备/串口Telnet测试 | 连接/保活/暂停/Collector/存储49项通过，覆盖真实SSH保活不重置十秒正文超时、EOF与预算续计、跨包前缀和小时命名；Linux CI34935808418验证真实Worker SIGTERM释放SSH连接/配额及新运行自动恢复；34/35历史证据保留 | 本地协议不等于真实Telnet硬件协商；物理断网、强制杀进程后的失联隔离、长期背压和非合作集群接管仍未验收 | 继续目标多节点故障矩阵；不再把已完成的正常进程退出、暂停/空闲矩阵列为开发缺口 |
| R09 初始化/定时队列、断线续计、重启归零、发送预算 | `commands/reservation.py`、`collection/runtime.py`，数据库原子性已实现 | 祖先提交`92e01d6`及真实Mongo验证；新增真实Telnet重连测试同run两session各执行一次、预算2，MongoMock回调仅证明续计语义 | socket 不属于数据库事务，设备执行结果仍可未知 | 保持 UNKNOWN、不补发；勿重复实现预算事务 |
| R10 手动优先、不跨会话、断线拒绝与审计 | `commands/manual_submission.py` 将入队、幂等映射和审计同事务提交；已加载本机API | 历史真实副本集证明审计失败/取消整体回滚、同键并发返回同一命令、提交确认丢失后只读恢复、停止后同键重放；临时数据已清理 | 最终 DB 检查至 socket 写入仍需物理隔离；历史 PENDING 仅重放，不补造审计 | 继续 R11 接管隔离 |
| R11 幂等启停、受控重启、租约/代次隔离 | `tasks/editing.py` 编辑、资源声明、停止操作、审计同事务，安全排队编辑保留RUNNING；Worker已知失败保持STOPPED | `175b938` Linux CI 34328314712通过；真实副本集回滚/竞争/确认丢失与排队调度验证；本机API/Worker已更新，34实机暂停恢复停止通过 | 跨节点物理隔离未证明 | 继续旧实例接管隔离，目标Worker部署需受控维护 |
| R12 PSH 密文、ls 探测、模拟口令、失败仅影响当次 | `collection/psh_*.py`、`common/config.py`、Docker/原生环境生成器及`docs/psh-production.md`；真实OAuth/itapi协议已实现，生产模式需.env启用并填凭据 | 本轮101项协议/部署/环境测试通过，原始source转发、token缓存、403003有限刷新、失败不盲发设备密码；参考ssh_debug.py核对，不调用真实服务 | 设备仍处于Password时先恢复命令通道；公司网络真实OAuth/itapi和10003切换未验证；监控ASH仍有10秒上层预算 | 公司部署填PSH_MODE=http及四项凭据后验证；默认disabled，本机mock不替代真实解密验收 |
| R13 10 MiB 编号分卷、上海小时、仅日志 tar.gz、归档后删原卷 | `logs/storage.py`、`logs/compression.py`；同小时批量正文/索引I/O，取消等待线程完成，部分写失败保留原卷并拒绝继续 | 新增4项批写/取消/短写/慢写故障测试；相邻35项；32路115200行合成归档摘要一致；本机120小包批写8.121→0.545ms，见[写入验证](history/2026-09-15-write-batching.md) | 集群验收未完成；10M按10MiB；准入默认200ms且可按R87逐节点配置，高延迟不主动丢弃已有批次，断电/永久磁盘故障不保证零缺失 | 更新目标Worker后实测写延迟与混合负载，保留文件可读P99≤200ms原始验收目标 |
| R14 小时查询、统一小时包、多选 ZIP、Range | `logs/hour_download.py`、`logs/export_output.py`、日志前端，已实现 | 下载/归档测试、历史浏览器下载 | 分布式缺片与规模限制待验收 | 多小时端到端校验 |
| R15 流式搜索、并发/读预算、配额与到期清理 | `logs/jobs.py`、限制器、`logs/maintenance.py:cleanup_exports`、`export_writer.py`、`export_readers.py`、`export_locks.py`；终态导出以关闭证据、读者租约和文件锁受限回收 | `test_maintenance.py`终态/拒绝条件、真实Mongo作业崩溃收尾及本轮取消/失联/读者清理竞争验证见R26 | 混合持续负载、真实大文件中断和目标环境未证明；无关闭或归属证据的取消/旧产物保留 | 在目标环境完成大文件和多人下载维护验收；不重复实现已有回收互斥 |
| R16 美观 UI、侧栏折叠/滚动、状态按钮、修改二次确认 | 前端app/features、useWorkspaceCollections/useWorkspaceNavigation/AppNavigation；App当前500行，5a0ff89已统一刷新入口 | 本轮核对导航12项、前端154项/构建通过；8b99fd4的Linux前端CI成功；既有桌面/移动端截图保留，不作为本轮新截图 | 全页面和全部分辨率不能由单一浏览器脚本证明；目标浏览器仍需部署验收 | 保持统一刷新调度；目标浏览器验证，不重复实现已经完成的导航合并 |
| R17 实时虚拟列表/限速、ANSI、暂停跟随与续传 | `LiveLogs.vue`、`LiveLogRanges.vue`、`logs/gap_catalog.py`、`gap_snapshots.py`、`shared/composables/liveLogBuffer.ts`；已支持跨文件/跨会话目录定位和短游标补读 | 浏览器精确五页补读、暂停/任务切换/迟到响应及1440/390/320视口；20项生产页面验证；`DELETED`缺口持久分页定向3项通过 | 无file/offset或目录不完整的源缺口只能转小时归档；物理跨节点与真实持续采集规模未验收 | 目标多节点部署后验证目录一致性和真实流；不重复实现既有补读窗口 |
| R18 保留天数、节点登记/准入、审计/事件 | `administration/settings.py`的保留期/节点配置与审计同事务，`node/input_admission.py`，`record_maintenance.py`，事件展示/筛选；已实现 | 节点输入速率真实Mongo竞争/恢复、设置保存、增长记录维护和20项生产页面验证通过；事件既有真实Mongo派生查询与浏览器证据保留 | 登记不等于部署；目标节点持续输入和生产事件/索引成本未验收 | 部署同版API/Worker/前端后，按目标吞吐标定阈值并观测维护与事件查询成本 |
| R19 Token/撤销/权限、加密审计、TLS | Token/公共鉴权、`users/sessions.py`；有效用户共享读取，写入按所有者，令牌继承绑定用户，旧独立资源/任务范围模型已替换 | 权限/脱敏/会话及服务令牌测试，本轮全量通过 | 分布式 TLS 未验收 | 真实代理与会话撤销验证，参见R34/R35 |
| R20 500×1200 行/秒×24小时，文件可读 P99≤200ms | 压测工具与写入指标，待验收 | 短时报告仅证明对应样本 | 无等规模证据，API 观察不能替代文件可读 | 独立文件探针与 Linux 集群全天验收 |
| R21 及时清理开发日志，保留证据 | `scripts/cleanup_dev_server_logs.py`、`dev_cleanup_evidence.py`；受限ID维护入口 | 历史清理1,028个副本/601,373,134字节；此前10归档/18 catalog/3,080,799字节预览PROTECTED；本阶段隔离实验自动清理；本次授权开发重建已删除本机旧日志、实验产物与数据库数据 | 后续失败实验仍需完整来源证据；一次性本机授权不推广到其它工程或正式环境 | 后续仅对新建实验产物运行预览和受限清理，不缩短保护或全局保留期 |
| R22 中文提交、部署/API/运维文档、持续总表 | `AGENTS.md`、hooks、CI、docs，部分交付 | 本轮总表和历史入口，既有提交校验 | 压缩率与全天容量报告未交付 | 每次同步对应状态行 |
| R23 五类Linux部署、自启动、重部署保留运行意图及配置/卷 | 部署入口、`deploy/*.yml`、`node/shutdown.py`、Worker.close；Worker容器120秒优雅窗口，先撤销准入、物理关闭再释放，不改用户运行意图 | 历史Linux完整及独立组件重跑证据保留；本轮真实Worker SIGTERM替换、同node新运行自动采集及源日志比对通过，监督相关63项通过，详见重部署记录 | 未物理重启宿主机；旧版本已停止任务与首次退出仍执行旧镜像的任务需升级后核对手动启动，不能猜测并批量开启；SIGKILL/永久磁盘故障仍需隔离 | 目标服务器升级Worker并验证后续重部署；不将正常重部署自动恢复推广到未知旧实例接管 |
| R24 正常登录、内置管理员、子账户及权限 | `users/`、前端 `features/auth/`、`app/AppNavigation.vue`；含当前用户与本人服务账号入口 | 历史Linux CI；本阶段会话测试、账号浏览器与隔离Cookie普通用户采集全链路通过 | 目标部署后的真实设备验收未完成 | 目标部署后验证正常登录、会话撤销和真实设备授权链路；保持会话代次隔离 |
| R25 平台来源 IP 白名单、多网段独立权限 | `access_policy/`、Nginx、前端 IP 管理，已实现并通过 Linux CI | IPv4/IPv6、权限交集、设备和串口目标不受限测试；CI 真实代理启用/关闭策略及伪造 XFF 检查通过 | 额外反向代理拓扑需单独配置可信来源链 | 部署时按实际代理链检查 clientIp |
| R26 平台访问记录、业务审计与凭据脱敏及日志作业崩溃收尾 | `logs/job_lease.py`、`job_execution.py`、`job_completion.py`、`export_writer.py`、`export_readers.py`、`export_locks.py`、`maintenance.py`实现持久执行归属、关闭证据、读者租约与回收互斥；内部令牌不公开 | 真实Mongo隔离子进程终止后FAILED/审计一次/不重放/片段保留/后续新作业完成；取消、过期、迟到终态、读者/清理竞争与失联恢复定向验证通过 | 旧无租约RUNNING及未知归属旧目录仍无安全判据，必须保留；真实大文件断电窗口、多人持续下载和目标部署未验收 | 验证目标环境长时运行和大文件故障矩阵；只对有关闭/归属证据的孤立产物受限回收，不重复实现已完成的事务与互斥机制 |
| R27 Ubuntu/Debian无Docker主机部署、五类入口、详细注释与自启动 | `deploy-native*.sh`、`scripts/native_*.py`、`deploy_native.py`、`docs/native-deployment.md`，已交付；Nginx权限和跨组件合同预检已修正 | `eb4e829` CI34338375626 Ubuntu24.04真实首次/重跑/重启通过，配置不变，临时安装已删除 | 未物理重启宿主机；Ubuntu22.04/Debian12自动依赖分支尚未实机验收 | 目标服务器按文档部署，验收实际依赖源和开机启动 |
| R28 其他电脑通过HTTP服务器IP正常操作 | 共用`frontend/src/shared/api.ts`兼容UUIDv4；Docker/原生前端均重新编译该源码；CI加入`verify_http_browser.mjs` | 先复现相同异常，前端52项/构建通过；真实非安全IPv4 Chrome成功发出启动/命令/下载模拟请求；CI34338375626两类部署通过 | 目标服务器需拉取并重新构建前端；模拟浏览器不等于全部实体设备业务验收 | 更新目标服务器前端并强制刷新，继续产品全链路验收 |

## 验收口径

新增需求对照：

| ID / 最终需求 | 实现位置与状态 | 验证证据 | 未完成部分 | 下一步 |
| --- | --- | --- | --- | --- |
| R29 内网HTTP节点登记保存、节点删除、完整/独立部署配置一致 | `administration/settings.py`、`node_lifecycle.py`、`tasks/claim.py`、设置前端、`deploy_env.py`；已实现并推送 | CI34341798389六作业通过，真实Docker节点登记与持续心跳；真实删除竞争/历史保留/迟到心跳保护；浏览器确认及1440/390截图；本机API/Worker更新后心跳正常 | 目标服务器部署未验收 | 按节点部署排障文档更新原服务，不改已有节点身份或日志目录 |
| R30 明确服务器UTC与上海小时归档的时间关系 | `LogArchives.vue` 标注北京时间UTC+8；原后端归档时区保持上海 | 2026-09-09 18:30:12 UTC归入2026-09-10 02点单测；浏览器同日小时样例显示通过 | 不变更服务器系统时区；不是自动跟随主机时区 | 保持日志前缀、小时目录和显示一致 |
| R31 日志工作台切换不横移、资源行快捷建任务 | `styles.css`稳定滚动槽及移动抽屉父容器宽度、`LogsWorkspace.vue`宽度约束、资源页与App复用任务编辑器；生产页面验证已加入CI | 本机及LinuxCI34346191768生产1440/390布局/提交/权限通过；后续CI34346496395中文截图已收取 | 目标服务器需更新前端 | 部署后复核目标浏览器 |
| R32 资源多条件查询与任务摘要、按时间查询、站内开放API平台 | `resources/api.py`、`logs/time_range.py`、`reference/`、`features/api/`；目录来自真实OpenAPI，支持权限/参数/请求响应示例与搜索 | API/资源发现/时间查询单测；文档1440/390/320浏览器通过 | 目标服务器尚未部署新版本 | 隔离全链路已通过；目标部署后验证 |
| R33 工作台分页选任务、资源筛选、状态/创建人/创建时间 | `LogsWorkspace.vue`、`LogTaskPicker.vue`、`LogResourceFilter.vue` | 工作台1440/390/320浏览器：resourceId筛选、状态轮询、迟到响应、北京时间与所有者按钮 | 隔离模拟链路通过，目标真实设备规模验收未完成 | 目标部署后验证资源筛选、分页和状态轮询 |
| R34 资源规范IP唯一；资源/任务共享读、创建者及管理员写 | `resources/address_claim.py`、`security.authorize_owner`、任务/资源事务、`shared/ownership.ts` | 单测及真实Mongo12并发仅1成功、占用回滚和软删除释放；审查所有权事务入口 | 本机旧库已在本次开发环境重建时删除；目标既有环境如有历史重复IP，须先受控核验；新增均阻止重复 | 目标既有环境在受控数据治理前核验重复IP，禁止误删历史日志 |
| R35 管理员管理账户；第三方绑定用户、永久有效和用户生命周期 | `users/service_tokens.py`、`users/sessions.py`、下载授权、账户前端；已加载本机API | 永久null合同；停用/删除/权限继承；过期PATCH不能复活；真实事务审计；账号浏览器通过 | 目标服务器尚未部署 | 目标部署验证 |
| R36 个人/指定用户多选共享/管理员全局模板，任务独立快照 | `commands/templates.py`、`users/share-targets`、模板前端、`tasks/creation.py`/`editing.py` | 模板共享与撤销/删除后快照、幂等重放单测；真实owner+name唯一事务；共享浏览器通过 | 目标服务尚未部署 | 部署后复验模板到任务流程 |
| R37 实时日志HTTP/HTTPS双协议与两种部署兼容 | `shared/websocketUrl.ts`供实时页及API示例复用；Docker与原生共用`deploy/nginx/frontend.conf`，TLS示例支持Upgrade | URL单测3项含端口/IPv6；WS路由ws/wss ASGI握手；文档浏览器；代理配置源码核验 | wss真实证书/目标代理尚未实机验收 | 隔离真实ws已通过；目标HTTPS需证书及代理验收 |
| R38 管理员/绑定用户可随时查看服务口令，管理权限不扩大 | `service_tokens.py`加密存储/查看/轮换、`AccessManager.vue`和本人账号入口；已加载本机API | 本阶段后端783项、前端70项；真实Mongo轮换回滚/未知提交/查看审计失败；本人/非本人/改绑/IP/无缓存测试；最终账号生产浏览器及HTTP复制回退通过 | 旧口令只有摘要无法还原，需管理员二次确认后重新生成；目标服务器待部署 | 目标部署后验证账户与服务口令流程 |
| R39 从模板切换其他导航后正文同步切换 | `TemplateList.vue`安全读取共享列表，`commands/templates.py:public_template`补齐读模型；已更新本机API | 缺字段历史夹具复现；修复后生产桌面3轮模板→资源/日志/API/配置及390/320无残留无控制台异常；后端定向17项通过 | 当前已异常的浏览器需刷新一次加载修复 | 目标部署 |
| R40 资源/任务/模板按权限多选批量操作 | `ResourceBatchDelete.vue`、`TaskList.vue`、`TemplateList.vue`与`bulkOperations.ts`；资源/模板批量删除、任务批量启停暂停恢复，串行逐项结果、确认/选择代次及卸载保护 | 单元测试；资源/任务/模板mock覆盖取消零请求、部分失败继续、状态/所有者过滤、页筛选清选与轮询保持；见交付记录 | 批量控制202仅表示已提交，实际执行依赖节点；不跨页批量选择 | 无本轮实现待办；目标环境验证批量控制从202到终态的链路 |
| R41 三页创建用户筛选、创建/删除时间、普通默认本人/管理员默认全部 | 三类GET支持createdBy；`users/creators`最小历史用户目录；`CreatorFilter.vue`与三页开关/时间列，统一Asia/Shanghai；模板软删除保留deletedAt | 后端组合筛选、最小字段、历史账号、软删除与快照测试；三页mock默认范围/切换/筛选/时间列 | 已物理删除的旧模板无法恢复；任务显示资源删除时间；软删模板名称仍占用 | 无本轮实现待办；目标环境验证创建人筛选和时间列 |

| 指标 | 实际测量 | 与原要求关系 |
| --- | --- | --- |
| 原要求文件可读 P99 | 采集端接收到独立读者可读，需明确按行/批权重 | 尚无 500 路全天证据 |
| `common/write_metrics.py` 的 WriteLatency | 首块接收至批量写入完成，批次数加权、滚动秒桶 | 不独立读文件，不含目录/API |
| `scripts/service_benchmark_latency.py` 的 BatchLatency | 源端发送至正式 API 读齐整批，行数加权，含轮询/排队 | 附加端到端指标，不是原文件指标 |
| API 直方图溢出 | 200ms 以上使用观测最大值给保守 P99 上界 | 非精确 P99，超标不能单独证明原指标失败 |

显式 `httpx.Limits(max_connections=...)` 的 `max_keepalive_connections` 为 `None`，不能套用默认构造的 20。依据依赖参数作判断前必须核对实际构造和安装版本。历史 64 路实验未通过 API 延迟与并发窗口，不能宣称容量达标。

## 后续验收

以下为持续目标尚未完成的验收，不代表已经交付。每轮先核对最新业务指令、本表、Git最新提交及工作区，再选择可执行的下一项；重新核实服务、任务和测试产物，禁止将本表的历史时点当成当前实时状态。旧的暂停指针和历史CI提交不再作为当前流程入口。

1. 目标服务器部署及真实TLS证书/代理、多机节点连通性验收；本机API/Worker已更新，34及35实机暂停恢复已验证，35新增真实空闲重连证据；专用测试任务已停止并清理日志。
2. R11跨节点物理隔离；R26日志作业已具备持久租约和明确失败收尾，后续补故障产物关闭证据与受限回收、真实大文件故障矩阵；已完成的数据库事务不重做。
3. R20 Linux集群500路全天容量、独立文件可读P99和压缩率报告。
4. R21后续新建实验产物按来源和到期证据受限清理；本机历史数据已在本次明确授权的开发环境重建中删除。

旧阶段提交、测试和进程记录见[历史验证记录](history/2026-09-09-status-legacy-validation.md)，不得当作当前进程状态。
