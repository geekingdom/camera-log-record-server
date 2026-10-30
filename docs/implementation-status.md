# 当前状态总表

| 当前需求（2026-10-30 下午，本机验收完成，CI夹具修复后复验） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 百设备并发订阅、频繁重启/重试，少数慢会话不拖垮节点 | `collection/runtime.py` 条件唤醒、内部 tail 有界等待、`write_pressure.py` 准入隔离、`write_diagnostics.py` 关联诊断及节点界面 | 真实HTTP/WS/Mongo：100订阅×3轮及令牌撤销，tail零残留；100写入器×3轮、两个阻塞时其余98完成，150MiB正文/600清单摘要校验；独立准入/生命周期审查175项通过；仅为本机合成与协议验收，不等于100实体设备长期容量 | 后端2213/前端226、构建/Ruff及35组生产浏览器通过；中文提交并等待最终HEAD完整CI；公司环境长期验收待部署 |
| 归档边界批写、摘要时机及索引隔离 | `logs/rotation_batch.py`、`storage.py`、`compression.py`、`background_execution.py`、`maintenance.py` | 摘要本已逐次成功写入增量更新；修复跨边界逐小块追加和默认线程池占满阻塞；归档/索引独立进程、后台预算80/20、索引有界去重提交；消失候选不算幻影积压，引用正文缺失仍报警 | 索引队列压力/归档优先及迟到回调复查完成；归档关联120项及全量通过；等待最终CI |
| API/Worker日志去重、诊断与动态文档 | `observability.py`、`websocket_logging.py`、`write_diagnostics.py`、归档生命周期和实际API模型 | API副写失败合并最终访问记录，业务/副写耗时分离；最慢会话及端到端/I/O/缓冲/执行器/事件循环有界诊断，保留异常栈与恢复；真实节点接口新writeAdmission返回及说明测试11项通过；前端226项/构建通过 | 全量及生产浏览器通过；真实接口返回覆盖通过；等待最终CI，不声称截图时刻根因已被日志唯一证明 |

首轮功能提交 `a8a4c15` 的CI八项通过；容器目录水位脚本绕过构造器时遗漏新增条件锁，已补齐验收夹具并以真实Mongo验证OPEN、尾部水位与READY发布通过，等待修复后最终HEAD完整CI。

本轮证据、设计和验证边界见[下午故障及写入隔离报告](history/2026-10-30-automation-write-isolation.md)。原始testLogs保持只读，截图16:08:58晚于日志终点；尚未部署公司服务器。后台预算仅在归档/索引间按80/20分配，搜索与快照用独立同值限速器，并非跨进程全盘总带宽上限。

| 当前需求（2026-10-30，功能提交完整CI通过） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 自动化并发后资源慢、反复加载、刷新回登录 | logs/live_subscription、logs/api；会话恢复、资源请求合并/取消和静默刷新 | 完整遍历API 6,969,813行、Worker 145,386行及HAR；旧版本并发回归失败，修复版48次CRUD/订阅断开与删除后无残留tail；身份成功不受业务初始化阻塞 | 公司部署及长时负载复验；详见[故障调查](history/2026-10-30-production-concurrency-incident.md) |
| 数据库查询与索引 | resources/listing、common/database、users/sessions、security、access_policy | 两个任务关联部分索引与一个待处理操作部分索引；真实Mongo升级/重复初始化及获胜计划验证；省略非UI路径令牌目录查询，同HTTP复用IP策略，WS及下一请求仍重查授权 | Mongo7 CI已确认扫描2000→500；公司实际索引目录/profile未提供 |
| Worker收尾及日志优化 | ssh_admission/runtime/worker、runtime_events、maintenance/storage、observability | 预建连未知申请按端点代次围栏及运行身份精确回收；真实Mongo竞争通过；重复ASH成功去重且失败后状态可恢复；候选spool消失不误报，引用内容丢失仍报警；HTTP INFO降噪及失败requestId关联 | 已建立连接未知关闭仍隔离；3条历史InvalidOperation缺栈无法定因，已补脱敏栈诊断 |
| API合同、回归与交付 | 实际WebSocket帧模型、动态文档、README/API/运维/数据库文档、CI | 后端2176项、前端225项、构建/Ruff、35组生产构建浏览器全部通过；真实Worker/WS/小时下载通过，1440/390截图目检 | 功能提交6d86792的九项CI全部通过；公司环境本轮未部署 |

功能提交 `6d86792` 的 [CI41009273392](https://github.com/geekingdom/camera-log-record-server/actions/runs/41009273392) 九项全部成功，含NFS、前端35组浏览器、后端、Mongo7真实聚合Explain/并发回收及容器/原生部署验收。专属Mongo仅剩系统库后已关闭，随机库和自建日志目录已清理；原始公司证据未改动。

本轮针对用户已部署公司的版本排查，以下历史记录中的“未部署”只描述各轮当时状态，不代表当前现场。本轮修改尚未部署公司服务器。

| 当前需求（2026-10-29，完整CI通过） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 下载记录展示创建人与实际日志时间 | job_submission/jobs、下载历史API、DownloadHistory | 创建人快照、实际成功分卷时间、前8段预览与按需详情；旧actor可靠回退；专项及浏览器通过 | 31组生产浏览器及完整CI九项通过 |
| 超级管理员/管理员/普通用户 | users/roles、models、sessions、api及账户界面 | 管理员不能修改其他管理员，允许本人；实际Mongo降权竞争、审计回滚及服务凭据保护通过 | 31组生产浏览器及完整CI九项通过 |
| 原六项基础权限新增固定手工命令 | commands/api、manual_submission、LogsWorkspace/TaskEditor | 跨所有者提交、用户名快照、有效账户围栏和幂等一次审计；真实Mongo验证通过 | 31组生产浏览器及完整CI九项通过 |
| 额外权限截止日或长期，到期保留基础和本人编辑删除 | users/roles、会话刷新、expired_ownership、资源任务入口 | 上海截止日结束边界、Cookie/Bearer实时收权、本人/共享/IP边界和浏览器通过 | 31组生产浏览器及完整CI九项通过 |
| 实际API合同与站内动态文档同步 | 请求模型、响应合同、reference目录与示例 | 后端全量2148项通过，覆盖目录、中文说明、真实响应及Python请求示例 | CI九项通过；公司环境部署待执行 |

功能提交 `eadb29b`、浏览器验收地址修复 `ae6fc59` 的 [CI40870064977](https://github.com/geekingdom/camera-log-record-server/actions/runs/40870064977) 九项全部成功。本机后端2148项、前端220项、构建/Ruff、31组浏览器场景及隔离真实Worker/WebSocket/小时下载通过；随机数据库、日志与专属Mongo已清理。真实事务验证及交付边界见[本轮验收](history/2026-10-29-roles-expiry-download-history.md)与[事务记录](history/2026-10-29-account-role-transactions.md)。公司环境未部署。

| 当前需求（2026-10-28，完整CI通过） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 登录失败审计记录填写用户名 | users/api、事件metadata合同 | 错误凭据及429保持anonymous主体、只记username；专项通过 | 后端2136、前端212、合同专项81通过；CI九项通过 |
| 资源默认全部、仅查看自己包含共享 | 资源查询与列表筛选 | 默认全部、互斥选择、独立包含删除；四组合真实Explain通过 | 全部20键20文档、本人共享21键20文档返回20；CI九项通过 |
| 已归档片段首屏和前后翻页加速 | logs/source、archive_readers、LogFileViewer | 首屏不扫描包尾；有界512KiB尾部命中重叠，前端8段缓存；30项归档回归通过；64MiB合成解压67MB降至270KB | 独立审查和归档30项通过；CI九项通过 |
| 日志分片上限512MiB | segment_size、设置模型与表单 | 10–510按5MiB步长、512端点，默认10不变；专项通过 | 完整30组浏览器通过；CI九项通过 |
| CPU/内存趋势导出图片 | ResourceMetricsDialog | 复用当前ECharts状态、2倍PNG含标题设备时间坐标轴图例；专项及目检通过 | 固定3200像素图片目检及30组浏览器通过；CI九项通过 |

功能提交 `f880c60` 的 [CI40678949123](https://github.com/geekingdom/camera-log-record-server/actions/runs/40678949123) 九项全部成功，包括前端与NFS验收，无配额例外。本机后端2136项、前端212项、30组生产构建浏览器、真实Mongo四组合Explain及隔离Worker通过；临时数据与专属进程已清理，公司环境未部署。详细结果与性能边界见[本轮验收记录](history/2026-10-28-audit-resource-archive-metrics.md)。

2026-10-26功能提交 `c31d147` 的前端趋势图验收时序问题已由 `6c5d3e0` 修复；[CI40266199567](https://github.com/geekingdom/camera-log-record-server/actions/runs/40266199567) 九项全部成功。后端2129项、前端210项、30组生产浏览器及真实Mongo事务/索引和隔离Worker通过，专属测试数据与进程已清理，公司环境未部署。

| 当前需求（2026-10-26，完整CI通过） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 已删除用户同名重建，按新配置及新身份生效 | 用户创建、局部唯一索引迁移、UserManager | 保留旧ID审计/令牌，不复活旧会话；活动同名409准确提示；专项通过 | 真实Mongo索引迁移、并发及后端2129项通过；后续合同44项/权限18项通过，CI九项通过 |
| 删除用户时删除资源或转移至任意有效用户 | users/api、write_gate、resources/lifecycle | 用户/资源/任务/审计同事务，源/目标写围栏，日志保留；无资源旧请求兼容 | 真实Mongo事务及150候选跨页/取消/迟到响应通过；浮层过渡后截图与宽度断言通过；完整30组生产浏览器通过，CI九项通过 |
| 仅有有效第三方服务账号显示API文档入口 | sessions、CURRENT_USER、workspaceNavigation、`tokens_active_user_expiry` | 有效/撤销/过期及管理员同规则，登录/me/改密刷新资格及无资格哈希回退通过；真实副本集 `sharing` 的随机库 Explain：有效账号1键/1文档、无有效账号0键/0文档，测试库和临时日志已删除；1440/390路由替身浏览器通过 | 后端全量2129、前端210、构建/Ruff、30组浏览器及真实隔离Worker通过；CI九项通过 |
| 命令记录显示手工发送用户名 | manual_submission、history、CommandHistory | 提交用户名快照，历史未知不冒认，定时标记系统；专项通过 | 1440/390/320新列浏览器通过；CI九项通过 |
| 默认不共享，资源显式指定一人或多人 | resources/sharing、请求响应合同、任务操作、ResourceShareRecipients | 共享不提升平台scope、不授予资源删除；事务复核共享撤销；真实Mongo竞争与id索引验证通过。生产构建路由替身覆盖默认空名单、多人选择、编辑保留、清空撤销、共享用户编辑/认证无删除、缺scope隐藏入口及1440/390无横向溢出 | 后端全量2129、前端210、构建/Ruff、30组浏览器及真实隔离Worker通过；CI九项通过 |

| 当前需求（2026-10-24，功能验收通过，上传配额例外） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 设备认证异常不介入Telnet串口采集 | `node/worker.py`、`resources/health_policy.py`及协议回归 | 既有创建/调度/恢复/重连豁免已核对；新增Worker与健康停机入口直接排除串口；新增入口测试先2失败2通过，修复后三坏状态及相关专项58项通过 | 后端2120、前端204、构建/Ruff、真实Mongo三坏状态周期/手工事务及28组浏览器、隔离Worker/WS/下载通过；7fdba35的CI40068374373七作业成功，前端/NFS功能步骤通过但上传配额失败，用户允许忽略上传问题；公司现场未部署 |
| 基于当前源码重写README及系统图表 | `README.md`、`diagram/readme/*.svg` | 已组织模块/目录/功能/部署/存储/权限/运维，并新增架构、ER、序列、健康与存储图 | 已核对源码、文档链接与六图渲染；详见history/2026-10-24-serial-auth-readme.md；最终4a0f2ff的CI40072615415九项全通过，详见本轮历史记录 |

| 当前需求（2026-10-23，历史趋势与日期提示完整CI通过） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 已删除资源的 CPU/内存历史可读且不显示旧采样租约 | `resource_metrics/api.py`、资源列表/趋势面板 | 已删监控关闭资源入口、删除前预设范围、无自动刷新、历史CSV及旧资源/任务租约排除；后端全量2114项、前端204项、构建/Ruff、28组生产浏览器、隔离真实Worker/WS/下载通过 | 功能df8e935的CI39946124081九项成功；不恢复采样，仍遵循指标保留期 |
| CPU/内存自定义日历标记有采样日期 | `ResourceMetricDatePicker.vue`、资源指标月份API及运行时模型 | 上海日期、软删资源、翻月、失败重试及1440/390截图通过；真实Mongo聚合Explain扫描3键3文档，复用既有索引；站内99操作文档自动生成 | 功能CI九项成功；详见history/2026-10-23-deleted-resource-metric-calendar.md；生产未部署 |

| 当前需求（2026-10-23，功能提交完整CI通过） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 同IP/序列号/型号设备重添保留Core历史发现时间 | `coredumps/resource_identity.py`、scanner、资源创建/认证事务 | 原文件ID/时间/事件幂等、旧目录升级、身份切换与冻结变化回归通过；真实Mongo24轮扫描删除竞争及身份索引Explain返回4/扫描5键4文档通过 | 最终后端2110项、前端204项、构建/Ruff、28组生产浏览器及真实隔离Worker下载通过；功能提交1fd54d2的CI39889511058九项全部成功 |
| 日志分片大小10–100MiB、5MiB步长、默认10 | 平台设置、`logs/segment_size.py`节点缓存、writer/下载及UI/API | 严格步长/默认/省略保留、真实Mongo审计回滚与CAS；35MiB实际分卷25→10及正文/时间名通过，整点增量路径与504MiB快路径合成通过 | 每秒后台刷新，新片生效、旧片收尾；不重写历史归档；详见history/2026-10-23-core-history-log-segments.md；完整CI已通过，公司部署复验待执行 |

| 当前需求（2026-10-21，日志片段实际时间命名完整CI通过） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 小时内.log片段名体现实际首末服务器接收时间 | `logs/naming.py`、写入封存/恢复、归档下载及片段UI/API | 最终后端2092项、前端204项、构建/Ruff、28组生产浏览器及桌面移动截图通过；真实隔离Worker/WS/小时下载、整点增量摘要、预算恢复及504MiB下载快路径合成验证通过 | 功能提交00533ed的CI39588453865九项全部成功；实现覆盖writer/恢复/下载及UI/API；公司部署复验待执行；详见history/2026-10-21-segment-received-time-names.md；小时包仍按逻辑小时命名，内部raw路径不做迁移 |

功能提交 `00533ed` 的 [CI39588453865](https://github.com/geekingdom/camera-log-record-server/actions/runs/39588453865) 九项全部成功。首轮仅报告上传遇 GitHub artifact 配额，按用户授权清理历史附件后重跑通过；未跳过任何校验。验收记录提交须确认其完整 CI 后交付，专属模拟数据与 Mongo 已清理，生产未部署。

| 当前需求（2026-10-21，响应异常修复与任务批量删除完整CI通过） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 全工程响应校验内部异常排查（含资源列表与小时归档） | 资源/Core/监控、日志/下载/检索、任务/命令/身份及节点/管理/事件域并行核对 | 已按四组领域核对实际写入、投影、历史字段与严格合同；发现并修复资源内部协调字段、日志文件部署恢复标记、下载清理原因及旧作业快照，未放宽未知字段校验 | 最终整合版本后端2069项、前端201项、构建/Ruff/差异检查、28项生产浏览器及后续专项均通过；84f4c85完整九项CI成功。公司现场升级复验待执行 |
| 小时归档与下载作业运行态字段隔离 | 日志文件公开投影、`common/database.py` | 已复现部署恢复文件标记与下载清理内部原因穿透公开响应；补正式路由200与敏感内部字段不回传回归 | 保持任务既有公开合同；最新旧快照专项48项及最终集成2069项通过，完整CI成功 |
| HTTP错误排障入口和安全字段诊断 | `common/response_validation.py`、请求日志与事件合同、审计页 | 真实500与requestId查询、安全字段路径/标准错误码、动态键碰撞及诊断辅助失败仍保留事件均通过；不记录原始 input、ctx、msg | 诊断专项与请求记录查询通过；前端201、构建及28项生产浏览器通过；真实隔离Worker/WS/小时下载通过并清理。首轮集成问题已修复，最终全量2069项和完整CI通过 |
| 资源列表严格响应合同的生产数据兼容性排查（2026-10-21，本地验证完成） | `common/database.py`公开投影、`resources/listing.py`、`response_resource_details.py` | 公司 `GET /resources` 报告 ResponseValidationError 但日志未含字段 detail；已从实际列表投影和所有资源写入器审计健康、资源监控、Core、输出恢复、从机引导及任务控制字段，修复内部健康租约、监控续租时间、健康恢复领取计数和从机端口可绕过既有剔除表的问题。真实资源文档写入全部审计内部键后请求列表，200响应、严格合同和字段不泄露断言通过；资源/Core/监控专项131项、Ruff和差异格式检查通过 | 不根据无 detail 日志断言生产唯一字段；待部署日志或原始 ResponseValidationError 的 `loc/type` 确认现场命中键，完整CI已通过，公司部署复验待执行 |
| API完整请求/响应字段中文释义与动态同源生成 | `common/api_contracts`、运行时Pydantic模型、OpenAPI、前端API文档 | 请求、JSON响应、全局/专用错误及WS首帧/输出帧同源；缺中文说明拒绝；98操作三尺寸浏览器通过；全部九项CI成功 | 后续接口按AGENTS模型规范维护；目标部署待执行 |
| 任务软删除、资源级联删除、默认隐藏及日志保留 | `tasks`、`resources`、调度与前端任务页 | 真实Mongo事务/索引/竞争、隔离Worker关闭及旧匿名下载链接206；任务页批量删除按冻结选择和版本串行调用既有DELETE，取消/改选零请求、部分失败逐项结果、删除后刷新及1440/390/320浏览器通过；完整容器与跨Worker CI通过 | 日志和制品仍按保留期处理；目标部署待执行 |
| 资源任务数与活跃采集排除软删、任务创建人独立列 | 资源聚合、看板、TaskList | 1440/390任务浏览器、统计/创建人/筛选回归及完整前端CI通过 | 目标部署待执行 |
| 常用命令分页/查询/状态筛选及批量操作 | `commands`、后台配置 | 事务CAS/逐行审计/幂等、真实Mongo三种Explain与确认选择代次回归；完整前端及容器CI通过 | 目标部署待执行 |
| 整点切换写入延迟尖峰优化 | `logs/storage.py`、增量索引摘要 | 保留fsync与spool恢复，去除锁内索引重读；8路注入40ms慢读合成P99 60.100→6.023ms，归档正文核对及CI通过 | 真实磁盘/NFS竞争及24小时容量仍须现场验证，不提高准入阈值 |

本轮功能提交 `84f4c85` 的 [CI39534266333](https://github.com/geekingdom/camera-log-record-server/actions/runs/39534266333) 九项全部成功：后端、前端、完整容器、组件部署、原生部署、跨Worker、NFS、Elasticsearch及中文提交规范。详见[10月21日验收记录](history/2026-10-21-response-errors-task-bulk-delete.md)。随机测试库、模拟日志与临时服务已清理，未部署公司服务器。验收记录提交亦须等待该提交完整CI后交付。

上轮功能最终提交 `42a48f8` 的 [CI39432146216](https://github.com/geekingdom/camera-log-record-server/actions/runs/39432146216) 九项全部成功：后端、前端、完整容器、组件、原生部署、跨Worker、NFS、Elasticsearch及提交规范。前两轮发现的响应合同遗漏和旧浏览器夹具已修复；详见[本轮验收](history/2026-10-20-task-delete-api-responses.md)。本机后端2057项与后续专项、前端201项、构建、27项生产浏览器通过；最终完整回归以该CI为准。

本轮自建随机库、模拟日志和验证服务已按来源收尾；未操作生产部署。验收记录提交后仍须等待该提交完整CI，不能以功能提交通过替代最终提交检查。

| 当前需求（2026-10-19，稳定Core直链与时间入参优化，功能提交完整CI通过） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 文件已稳定的Coredump按ID直接公开下载，无需先导出 | `coredumps`、前端Coredump列表 | 直链/过期回退/Range与If-Range/变更中止通过；桌面移动下载和复制不创建导出 | 功能提交e8089c8九项CI通过，待部署 |
| API时间入参无需时区，默认北京时间 | 共享时间解析、查询接口、`reference` | 各时间入口回归通过，API文档1440/390/320通过 | 内部UTC及响应兼容；九项CI通过 |
| 下载作业兼容hourIds并支持start/end覆盖小时范围 | `DownloadCreate`、`logs/job_submission.py` | 真实Mongo四小时范围、等价hourIds重放与事务通过 | 极值/168小时/缺失回归通过，九项CI通过 |

功能提交`e8089c8`的[CI39254595665](https://github.com/geekingdom/camera-log-record-server/actions/runs/39254595665)九项全部成功，包含完整前端浏览器与真实容器事务/下载链路。验收记录提交的检查继续在交付前确认，生产环境未部署。

本轮后端全量1988项通过，随后53项专项通过；前端198项和构建、Ruff通过，真实隔离Worker/WS/归档下载通过。详见[本轮记录](history/2026-10-19-stable-core-time-inputs.md)。

交付流程：完成集成、全量及浏览器验证后中文提交并推送，等待本次完整CI，失败修复后重新验收；用户状态询问或上下文压缩不取消此流程。


| 当前需求（2026-10-19，功能提交完整CI通过） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 从机每60秒ifconfig身份巡检，不符关闭并重新切换 | `collection/slave_identity.py`、`collector.py`、`slave_shell.py` | 正常/异常IP、旧输出隔离、LF/CRLF分包、停止取消、单队列及Runtime重连专项129项，真实AsyncSSH重连通过 | 实体设备部署后观察；周期巡检不能追溯恢复旧的混入日志 |
| 资源监控折叠及新增项自动定位 | `ResourceMonitorSettings.vue` | 正式浏览器1440/390折叠、聚焦视口、保存及跨模块草稿通过 | 前端198项及构建通过，待部署 |
| 管理员常用命令表格及全员工作台补全 | `commands/common.py`、`CommonCommandsSettings.vue`、`LiveLogs.vue` | 幂等/版本/权限回归；真实Mongo事务及两种默认列表Explain各100返回/100键/100文档通过 | 分页/编辑/补全迟到响应回归通过，待部署 |
| Coredump和下载记录归档公开附件链接，API文档同步 | `logs/api.py`、`coredumps/api.py`、`common/public_downloads.py`、`reference`及下载UI | 匿名/失效令牌/IP策略/过期回归；真实无Cookie Range206及16字节通过；96操作API文档1440/390/320通过 | 文件状态及保留期继续有效；功能提交6ba57ce九项CI全部通过；公司实体设备待部署观察 |

功能提交`6ba57ce`的[CI39192878168](https://github.com/geekingdom/camera-log-record-server/actions/runs/39192878168)九项全部成功，包含后端、前端、跨Worker、三种部署、NFS、Elasticsearch及提交规范。验收记录提交后仍须等待该提交CI结束；不以本机验证或推送成功替代完整交付。

本轮后端全量1963项通过，后续公开下载专项16项、API/常用命令32项通过；前端198项及构建通过。最终证据与边界见[配置与公开附件记录](history/2026-10-19-settings-commands-public-downloads.md)及[从机身份巡检](history/2026-10-19-slave-identity-probe.md)。

| 当前需求（2026-10-16，进行中） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| outputClose收尾与logread超限恢复、ES独立旁路索引、Top10、下载历史及独立制品保留 | `collection`、新`indexing`、`logs`与前端工作台 | 真实ES故障/重放/缺块/归档重建、Mongo Explain与租约CAS通过；PSH安全通道拒绝不终止采集回归通过；前端197项/构建/全套浏览器通过 | 共享归档一次逻辑限速读取及真实runner补建/删除验证通过；后端1945项、最终98项专项、Ruff通过；源码05f90be九项CI38878143703全部成功；见`docs/history/2026-10-16-indexing-downloads.md` |
| API名称消歧与北京时间统一 | `reference`与时间序列化/存储边界 | 明确字段HTTP/WS上海时间、原始index时间与历史UTC目录查询回归通过；API名称跨模块审查完成 | 全量及专项通过；源码05f90be九项CI全部成功。Mongo BSON/内部hourId保持绝对时刻语义；完整.env副本及三个组件Compose解析通过 |


| 当前需求（2026-10-15，API文档核对与Python示例，实现完成） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 核对最新路由/权限/参数及业务指南，为每个接口提供可复制Python调用示例 | `reference`目录、前端`features/api`、`docs/api.md` | 89个操作（88 HTTP＋1 WS）与路由一致，全部语法及HTTP替身执行/请求模型校验通过；修正日志月份参数示例和认证恢复说明；前端195项、构建、Ruff、1440/390/320复制与布局、真实隔离Worker/WS/下载通过 | 最终本机全量1905项、API专项27项通过；远端验收以本分支最新GitHub CI为准，公司部署后按实际权限配置示例。见`docs/history/2026-10-15-api-python-examples.md` |

上一轮最终提交`92e17e9`的[CI38538824171](https://github.com/geekingdom/camera-log-record-server/actions/runs/38538824171)八项全部成功，资源看板与主从保活已交付；其下旧“等待CI”文字为历史阶段记录。

本轮源码`84d53dc`的CI38684047001八项成功；记录提交81ba6dd的CI38684592721暴露资源监控浏览器悬停时序失败（其余七项通过）。已用800ms遮罩复现并改为等待canvas可交互后悬停，同一实验通过，保留全部数值与缺失值断言；详见本轮API文档验证记录，最新修正继续由完整CI验收。

| 当前需求（2026-10-14，资源看板与主从保活，集成验证中） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 资源看板首导航及默认首页，动态统计与企业运行概览 | 后端`dashboard`与前端`features/dashboard`、工作区导航 | 已核对现有权限、目录统计与导航；按上海自然日统计原始日志，不扫描正文 | 后端最终全量1894项通过；前端195项及构建通过；1440/768/390/320无顶部遮挡及溢出、刷新竞态/权限/快照保留通过；真实Worker隔离下载验收通过；生产浏览器套件全部通过；中文提交后等待完整CI |

| 主从SSH应用层输入保活，避免持续输出仍自动登出 | `collection/shell_keepalive.py`、`collector.py`与`command_dispatcher.py` | 真实AsyncSSH持续输出+输入空闲自动登出模拟通过；66项保活/命令/debug/从机专项通过 | 60秒无实际命令输入才空操作保活，发送前二次校验；串口不启用；全量测试及CI中，实体设备待部署观察 |

本轮`1375e4a` CI发现固定9月请求事件样本超过30天TTL、768宽度看板控制组及图表尺寸收敛问题。已改为当前UTC样本和相对查询范围、900px以下纵向头部与基于几何条件的严格浏览器验收；最终后端1894项、Ruff、构建及看板生产浏览器通过，等待修正提交完整CI。

上一轮最新提交`2035a4b`的[CI38512329030](https://github.com/geekingdom/camera-log-record-server/actions/runs/38512329030)八项已全部成功。下方旧轮次表内“等待CI”为该轮当时记录，本行结果为其最终核验。


| 当前需求（2026-10-14，实机PSH从机身份与401重连保护，验证中） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 从机debug参数错误被误作挑战等待；主从PSH回退不能混入主机日志 | `psh_dialogue.py`、`slave_shell.py`、`command_dispatcher.py`、`collector.py` | 35实机从机1/2复现debug无参数后超时；修正后初始化/手动/定时debug均成功；两轮新连接均重新验证.164/.166，退出边界被捕获 | 实机三入口及退出、严格关闭失败回归通过；后端全量1873项通过，等待本次CI |
| 从机身份不能沿用旧连接缓存，主从mock口令隔离 | `slave_shell.py`、`psh_passwords.py` | 外层HOST、内层SLAVE独立对话及口令键；目标绑定、外部主机IP拒绝、分包结束标记回归 | 本次公司HTTP接口仍不可达，实测使用提供口令的受限本机mock |
| 资源401且日志停止后不得反复重新登录；认证成功后自动恢复 | `collection/resource_auth_gate.py`、`runtime.py`、`resources/health.py`、`health_policy.py`及资源API | 真实Mongo/API/Worker/SSH验收通过：401保留活连接，断线无新登录，ERROR不解锁，SUCCESS恢复；用户停止不复活 | 旧数据恢复/停止暂停/旧owner/数据库超时回归通过；后端全量1873项通过，等待本次CI |
| 内存监控缺失进程不能显示0，数值最多两位小数 | `resourceMetrics.ts`、`metricAxis.ts`、`ResourceMetricsDialog.vue` | 后端稀疏采样缺项不写零、真实VmRSS零保留；null格式化修正和尾点缺失摘要回归 | 真实悬停492.99/42.88MB、缺失暂无数据、真实0MB均通过；191项前端测试与构建、1440/390截图检查通过 |

本轮详见[PSH从机实测与保护](history/2026-10-14-slave-psh-identity.md)。上一轮最新提交`c2a6de1`的[CI38410325125](https://github.com/geekingdom/camera-log-record-server/actions/runs/38410325125)八项全部成功；本轮修改必须提交后等待新CI完整成功再交付。

本轮提交`b8ed918`的CI七项成功，cross-worker-read旧ISAPI验收仍把401后转离线的停止原因断言为OFFLINE而失败。现按持久401策略严格验证AUTH_FAILED、锁存仍在且SSH登录次数为1；真实Mongo/API/Worker复跑通过，54行归档正文逐行一致。修正提交继续等待完整CI。

| 当前需求（2026-10-13，协议初始化、从机恢复及晚间归档补偿，本机验证完成） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 主机SSH/Telnet设备尽力自动ASH；串口仅显式debug；从机须ASH及精确IP验证才采集 | `collection/collector.py`、`command_dispatcher.py`、`slave_shell.py` | 从机64项、真实网络暂停重连7项通过；35主机ASH及两次从机192.168.253.164实测通过，临时连接关闭 | 后端全量1840项通过，后增慢库进度回归在9项目录专项通过；公司PSH以模拟协议回归覆盖 |
| 从机连接失败固定10秒重试；认证恢复成功立即唤醒仍RUNNING的SSH主从任务 | `collection/runtime.py`、`resources/health.py` | 运行/资源61项通过，停/暂停/采集中及旧owner不误唤醒 | 公司服务器部署后复核认证变化唤醒 |
| 晚间旧小时仍显示写入、节点无归档队列；重启历史补偿及完整校验优化 | `logs/archive_receipts.py`、`archive_catalog_recovery.py`、`maintenance.py`、`compression.py`及runtime目录CAS | 归档119项、目录9项通过；持久待办5秒泵、迟到OPEN及保留期保护；480MiB合成0.940→0.710秒、完整SHA一致 | 目标服务器升级后观察积压补偿；现场日志只覆盖至13:34，晚间根因不能仅据前端状态断言 |

CI追加修复：`f6eb8d5`的cross-worker-read复现双节点相同相对路径目录ID冲突；新分卷ID加入节点命名空间，历史归档补偿兼容本节点旧ID，保护其它节点记录。真实Mongo/SSH双Worker暂停恢复复验通过，逐行正文、同run及累计命令预算均一致；新增双节点归档回归通过。后端全量1841项通过、1项被本轮临时Mongo占用端口影响；清理后部署专项13项全部通过，目录专项10项通过，Ruff通过。最新修正提交仍须等待全部八项CI通过。

CI后续验证：`66195c9`已通过跨Worker暂停恢复及其余七项作业，故障转移脚本因模拟源暂停业务流时同时阻塞登录横幅而失败。已将首条ASH横幅与业务闸门分离，横幅仍计入逐字节验证；新增真实SSH回归旧实现失败/修正通过，双Worker故障转移与SIGTERM重启本机复验通过；单节点归档验收改按新run等待，防止旧READY误满足条件，新增回归覆盖。

CI模拟源语义补齐：`5e6bd7f`通过转移与恢复后，ISAPI完全无输出验收因每次重连仍有登录横幅而刷新存活失败。模拟源新增完全静默模式（包含重连横幅），该验收明确使用完全静默；业务暂停仍允许新连接握手。三项真实SSH/归档等待回归通过。

本轮工作记录见[协议初始化与实机证据](history/2026-10-13-protocol-ash-identity.md)和[晚间归档补偿与测量](history/2026-10-13-evening-archive.md)。前端190项及构建、全仓Ruff通过。本次提交要求GitHub八项CI全部成功后交付，实际运行结果以该提交的GitHub检查及交付消息为准；公司现场积压消化与PSH仍需部署观察。

| 当前需求（2026-10-13，从机debug与公司npm构建，完整CI通过） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 公司前端Docker构建使用内网npm源 | `deploy/frontend.Dockerfile`、两种前端Compose、环境样例及CI | npm ci之前配置公司registry及strict-ssl=false，现有.env无需修改；可选两参数透传、CI覆盖公网源；部署61项及Compose展开通过 | 公网Docker完整/分组件构建CI通过；公司镜像源仍需现场验证 |
| 从机三种debug入口自动解密、仅使用debug切换 | `collection/collector.py`、`command_dispatcher.py`、`psh_dialogue.py` | 标准PSH路径原已共用；复现缓存ASH跳过initial/scheduled调试，改以当前从机响应确认；最多10秒确认交接横幅后开放命令，失败进入重连；删除旧备用命令 | 从机1至3三入口/陈旧模式/延迟横幅等72项通过，后端全量1811项、前端190项及构建/Ruff通过，源码f89c1bd八项CI全部成功；无公司实机，不能断言现场唯一根因 |


源码提交`f89c1bd`的[CI38380387722](https://github.com/geekingdom/camera-log-record-server/actions/runs/38380387722)八项全部成功，包含两种Docker部署及cross-worker-read。完整公司.env副本在仓库外生成，保留原env.txt全部内容，仅追加两个可选npm构建参数。详细验证与边界见[从机debug与npm构建记录](history/2026-10-13-slave-debug-npm.md)。

| 当前需求（2026-10-13，源码与完整CI验证通过） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 重部署停止旧Worker/prepare失败 | `scripts/deploy_component.py`、`deploy_health.py` | 修复10秒单命令预算与120秒停止宽限冲突；停止150秒、各阶段独立预算、脱敏诊断；61项部署测试、真实Mongo受控部署与计划重启事务通过 | 公司服务器使用原.env和持久数据目录重新部署验证；未知旧连接仍需关闭证明 |
| 归档排队264项而处理中0，单失败不能堵塞其它归档 | `logs/compression.py`、`process_progress.py`、`maintenance.py`、`storage.py`、`node/worker.py` | 取消释放去重身份；独立5秒补偿、总8/实时6预留恢复名额、按年龄混合调度、失败30至300秒退避；真实FIFO卡住后同队列2个正常包完成，修复首项后重试完成；无锁进度、空闲池与不可终止进程边界回归；60分卷整批通知通过 | 源码提交f1e1699八项CI全通过；磁盘永久故障或缺失原文不能自动补造，公司积压消化仍需升级后观察 |
| 服务运行日志每日目录及到期空目录清理 | `common/observability.py`、`logs/maintenance.py`、`SettingsManager.vue` | 上海日目录、20MiB分片、7天默认与后台热配置；旧编号分片兼容、未知文件/软链接保护、旧活动句柄保留及权限诊断回归通过 | 新格式自升级后生效；旧根活动文件保守保留，避免滚动升级误删 |

源码提交`f1e1699`的[CI38364053528](https://github.com/geekingdom/camera-log-record-server/actions/runs/38364053528)已完整结束：cross-worker-read、backend、frontend、native-smoke、container-smoke、component-smoke、nfs-smoke及commit-messages **八项全部成功**。最终CI后端全量 **1790项通过**；本机前端 **190项通过**及生产构建、Ruff通过。真实API/Worker/WebSocket/小时下载、双Worker历史读取、唯一节点自恢复、部署与计划重启事务通过；归档公平性与恢复专项73项通过。八路合成预算恢复8.117→1.280秒、逐字节一致，1440/390浏览器截图已检查，临时数据库与合成日志/下载副本已清理。公司真实积压消化仍需升级后观察，详见[10月13日排查记录](history/2026-10-13-worker-archive-redeployment.md)。

| 当前需求（2026-10-12，本机验证完成） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 公司 Worker 写等待/归档积压、单故障扩大及重部署恢复 | `collection/buffering.py`、`runtime.py`、`logs/write_io.py`、`archive_spool.py`、`compression.py`、`maintenance.py`、`node/deployment_recovery.py`及部署入口 | API与Worker现场日志均已分析；实际文件I/O独立计量，持久封存不等待压缩槽，压缩进程损坏可重建、毒化归档退避，目录通知有界且5秒超时；真实Mongo单节点回退峰值1连接、新旧run不同且归档READY；部署证明/最终CAS回滚/暂停预算与partial索引Explain验证通过 | 部署迁移/暂停竞争回归通过，源码修正提交1c64a6a的八项CI全部成功；公司现场升级效果仍需观察 |
| API/Worker运行日志按时间保留，默认7天，按日20MiB分片并后台热配置 | `common/observability.py`、管理设置及生命周期、`SettingsManager.vue` | 前端190项和构建通过；1440/390设置截图检查通过；中文异常截断子进程超时回归、Python优化模式预算校验通过 | 管理员在“平台与容量”保存，Worker最长约60秒同步；历史被覆盖日志无法补回 |
| 24小时已压缩归档批量下载准备耗时 | `logs/hour_download.py`、`runtime.py`、`verify_archive_download_fastpath.py` | 504MiB合成原文/21.65MiB压缩：旧确认加复制0.536秒，新路径0.424秒；确认阶段约0.116→0.003秒，逐成员SHA一致；固定inode和发布竞争回归通过 | 保留真实压力下后台预算，432MiB在1MiB/s下仅复制就需约7.2分钟；不宣称公司下载耗时已解决 |
| 海康资源监控仅SSH主机与Telnet设备生效，排除从机及Telnet串口 | `common/resource_monitoring.py`、`collection/runtime_monitors.py`、`coredump_lease.py`、`resource_metrics/runtime.py` | 资格统一到运行、装配、租约与采样入口；独立审核及相关113项通过 | 既有SSH缺省target按HOST兼容；公司真实设备验证仍需升级后观察 |

最新后端全量 **1766项通过**，前端 **190项通过**，生产构建及全仓Ruff通过。真实隔离API/Worker/WebSocket/小时下载、双Worker缺口读取、唯一节点恢复、计划重启事务与受控部署事务验证通过；归档预算八路合成校验8.126→1.283秒、逐字节一致。详细现场证据、下载对照及限制见[本轮记录](history/2026-10-12-worker-stall.md)。全量后新增部署迁移/优雅暂停及发送前未知异常3项回归通过（相关12项通过）；真实部署事务复验通过。源码提交`210c96b`的[CI38271748138](https://github.com/geekingdom/camera-log-record-server/actions/runs/38271748138)七项通过，仅container-smoke定时预算事务验收沿用旧的吞异常预期而失败。已改为明确验证ReservationUncertain传播及无设备发送/唯一UNKNOWN/预算不退款，真实Mongo复验通过；修正提交`1c64a6a`的[CI38273257602](https://github.com/geekingdom/camera-log-record-server/actions/runs/38273257602)已完整结束，cross-worker-read、backend、frontend、container-smoke、component-smoke、native-smoke、nfs-smoke及commit-messages八项全部成功（2026-10-13核验）。

| 当前需求（2026-10-09，本机验证完成） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 小时归档日期选择器提示当前任务哪些日期有有效日志 | `logs/api.py`、`ArchiveDatePicker.vue`、共享API | 后端1707项、前端189项及生产构建通过；1440/390日历标记、翻月、切任务和失败重试专项通过；真实API/Worker/WebSocket/下载验收通过；Mongo Explain命中任务/小时索引 | 有日志日期右上角圆点，查询失败明确提示；切任务与翻月隔离迟到响应。Docker启动后的真实隔离采集验收已通过；生产UI套件全部23个脚本分段复验通过；提交99b7619的CI仅frontend移动日历即时溢出断言失败，其余七项通过；已改为有界等待弹层重定位，修复提交43d6583的[CI37875796250](https://github.com/geekingdom/camera-log-record-server/actions/runs/37875796250)八项全部通过，记录见`docs/history/2026-10-09-archive-calendar.md` |

本轮源码提交`2df6017`的[CI35729933565](https://github.com/geekingdom/camera-log-record-server/actions/runs/35729933565)八项全部通过，含新增单节点恢复和真实Mongo回收验证。以下本轮新增CI核验已完成；公司物理服务器升级观察仍待现场进行。

节点性能可读性优化提交`cadc80a`已补齐连续压力采样和最低后台预算边界：节点看板区分“压力采样中”“当前压力”“等待恢复”“恢复中”“已恢复”“预算已达下限”和“手动参数”，并显示当前有效后台读取预算相对基准的比例；后端心跳新增`pressureActive`、`pressureSamples`、`budgetReduced`、`baseline`和`recoveryStableSeconds`，不改变原有准入阈值和自动降速策略。前端189项、后端性能专项22项、生产构建及[CI35807642046](https://github.com/geekingdom/camera-log-record-server/actions/runs/35807642046)八项全部通过。

| 当前故障与稳定性审查（2026-09-22，本机验证完成） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 手动命令慢查询拖住监督周期，被误判控制面失联并全节点停采 | `node/worker.py` | 慢查询挂起时tick继续完成；任务快照失败不刷新存活；查询失败后可重试；最终后端全量1704项通过，独立复审无阻断 | 已改为完整归属快照成功刷新存活、每任务独立命令作业、最多8路查询且每次查询限时3秒；公司现场升级后观察 |
| 自围栏后唯一健康节点永久被排除，任务停在等待调度 | `node/failover.py`、`tasks/scheduler.py`、`tasks/claim.py` | 现场任务确有精确`AUTO_FAILOVER_FENCED`收据及唯一节点排除；真实Mongo/回环SSH单节点恢复、双节点迁移均峰值1连接，新旧run不同且归档READY；旧记录缺少释放时间及Worker时差回归通过 | 其它合格节点优先；无其它时原节点连续健康30秒可回退，超过15秒观察间隔重新计时；旧记录由Mongo补记观察起点。公司物理环境尚未验证 |
| 单个故障转移收据事务异常中断整轮普通调度 | `node/failover.py` | 注入`ConnectionFailure`，失败者保留旧归属和锁并退避，普通任务同轮领取；相关83项通过 | 已隔离逐候选消费、围栏预留及退避清理异常；普通领取提交未知时仍结束周期并重新核对容量 |
| 上轮计划重启恢复缺少任务级实例绑定和原子收尾 | `node/recovery.py`、`node/shutdown.py`、`logs/maintenance.py`、`scripts/verify_planned_restart_recovery.py` | 真实Mongo副本集验证完整收据成功、旧token与无收据拒绝、最终CAS失败全部回滚；仅有`.pending.json`而尚无tar的中断窗口可恢复；临时库及日志已清理 | 已移除`plannedRestart`永久豁免隔离；未知旧连接仍不能自动释放。任意缺少待处理清单的原始日志不属于本轮恢复证明 |
| 小时下载解压暴露随机`.partial`名、未归档小时目录多余 | `logs/archive_access.py`、`compression.py`、`hour_download.py`、`jobs.py`、`export_output.py` | gzip头无FNAME、小时包仅平铺日志、旧临时路径兼容、新格式完整小时免重压回归；真实API/Worker/WebSocket浏览器小时下载通过，控制台异常0 | 新下载修正旧包临时名；已下载本地文件不改变。新格式完整小时仍直接复用；旧格式按下载重建，有额外读取与压缩成本 |

现场任务响应已确认唯一节点排除导致无法重新调度；10秒监督超时前的Mongo变慢原因仍需公司环境观测，不能认定为唯一触发源。节点0写入样本/0积压不等于采集成功。本轮不提高缓冲、写延迟阈值或放宽未知旧连接隔离要求。前端183项、类型检查/构建及全仓Ruff通过；真实事务和单节点恢复验证已接入CI，新增提交CI按新SHA核验。具体证据及限制见[本轮验证记录](history/2026-09-22-collection-recovery-downloads.md)。

| 当前需求（2026-09-20） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 单节点重新部署时运行任务不再长期停留等待隔离；优先转移到其他可用节点，无其他节点时等待原节点恢复后自动重新采集；任务页支持批量重新启动等待隔离任务 | `node/shutdown.py`、`node/recovery.py`、`node/app.py`、`tasks/scheduler.py`、`frontend/src/features/tasks/TaskList.vue`、`taskActions.ts` | 计划重启恢复与普通失联不误恢复专项29项通过；前端生命周期10项通过，前端构建通过；恢复后任务保留`desiredState=RUNNING`并清理旧锁/run，调度器按可用节点排序；只有旧Worker写入`shutdownCompletedAt`后才允许批量恢复 | 需要在实际 deploy-all 单节点/双节点环境执行一次重部署验收；未知失联、旧Worker未完成收尾仍要求隔离证明，不自动接管 |
| Worker 重启后继续完成未归档小时日志 | `node/app.py`、`logs/maintenance.py`、`logs/compression.py` | `recover_orphan_archives` 维护测试覆盖待提交归档恢复、已发布归档重登记和重复运行幂等；Worker 启动在重新开启监督周期前执行该恢复 | 真实生产中断压缩后的大文件恢复仍需部署环境容量验收；恢复失败保留待处理文件并记录异常，不删除原始日志 |

本次计划重启恢复会使用节点持久化的`shuttingDownAt`、`shutdownCompletedAt`、`shutdownToken`和`accepting=false`作为联合条件，避免把普通节点失联或强制终止误判为可恢复。恢复阶段不直接绑定原节点：任务回到待调度状态，其他健康节点优先领取；原节点没有替代节点时，等新心跳恢复后再领取。

| 当前CI修复（2026-09-20） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 资源身份筛选浏览器验收在慢响应时读取旧表格，第二页多选断言偶发失败 | `scripts/browser_resource_identity_filters.mjs` | 原脚本在600ms响应下复现旧表格断言失败；修复后相同延迟专项及生产页面22项全量回归通过，保留两条选中及批量删除可用断言；语法/差异检查、独立审查和[CI35505458845](https://github.com/geekingdom/camera-log-record-server/actions/runs/35505458845)八项全通过 | 等待请求匹配、加载结束及目标IP列表渲染；逐行点击可见复选框。已完成 |

失败提交`2921616`的[CI35502758638](https://github.com/geekingdom/camera-log-record-server/actions/runs/35502758638)仅frontend失败，其余七项成功；修复提交`f7002fd`的[CI35505458845](https://github.com/geekingdom/camera-log-record-server/actions/runs/35505458845)八项全部通过。原因及验证见[CI修复记录](history/2026-09-20-resource-filter-ci.md)。本次仅调整验收同步方式，未改采集或资源页面业务逻辑。

| 当前问题（2026-09-20，已修复并完成本机验证） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 8路AUTO节点降至1MiB/s后小时归档长期积压 | `node/performance.py`、`performance_runtime.py`、`logs/compression.py`、Collector/Worker写入指标 | 后端1688项；八路512KiB真实压缩冻结旧预算8.124s→执行前刷新1.265s，解包逐字节及子日志清理验证；独立审查无阻断 | 已拆分实际I/O与批等待、分类原因、比例恢复、执行前读取预算和归档优先。现场硬盘争用/大日志混合负载未测；新提交CI另核 |
| 节点归档告警不能区分排队与处理中，偶发2秒写延迟口径不清 | `performance_runtime.archive_snapshot`、节点健康与NodeList、`common/write_metrics.py` | 前端183项/构建/Ruff，1440/390归档和实际I/O截图；秒级精度先红后绿；真实隔离Worker实时及下载通过 | 已展示等待/执行和I/O；1至60秒改100ms上界。阈值1至60000ms保留，500ms不自动提高，不承诺持续过载无源端丢失 |

证据见[归档压力排查](history/2026-09-20-archive-pressure.md)；8路×1200行/秒×2秒共19200行独立文件可见P99=119.478ms且源/读者/归档摘要一致，此为本机短测非24小时容量证明。模拟目录及随机库已清理。本机原任务均STOPPED、活动日志作业/导出为0后重载API和Worker，8000/8001/5173健康200，新归档分阶段与实际I/O字段已上报。

上一阶段批量资源提交`82d82df`的[CI35501714327](https://github.com/geekingdom/camera-log-record-server/actions/runs/35501714327)已确认成功；本轮归档修复须按新SHA独立验证。

| 当前新增需求（2026-09-20，已完成本机专项验证） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 新建设备资源名称非必填，省略或空白时默认规范IP | `resources/models.py`、`ResourceEditor.vue` | 模型与API测试；浏览器串口名称留空仍保存；后端1674项、前端178项通过 | 已实现，两类资源均适用；编辑原资源保留名称校验 |
| 仅海康网络设备按起止IP批量认证和添加，逐行改名改密码、删除和重新认证 | `resources/batch_creation.py`、`ResourceBatchEditor.vue`、`bulkResource.ts`、站内API目录 | 真实Mongo验证地址/审计失败整批回滚、并发同键、丢失提交确认恢复及加密；1440/390浏览器验证失败阻止保存、改密重验、共用凭据确认、删除确认、同键重试；类型检查/构建/Ruff通过 | 已实现，单批最多256地址、单批4路并行认证；后端重验全部成功才原子保存。未在真实设备执行批量认证；新提交CI独立跟进 |

证据与边界见[批量资源验证](history/2026-09-20-resource-batch.md)。真实隔离API/Worker/WebSocket/小时下载回归通过，随机库及合成日志已清理；真实Mongo验证和批量浏览器专项已加入CI。本机API已重载，健康200且OpenAPI包含批量接口，前端仍为`http://127.0.0.1:5173/`。生产构建全页面回归本轮全部通过，包含范围重建确认取消保留原行；新增提交CI以对应SHA单独跟进。

上一轮独立文件探针提交`7a36d23`的[CI35500093386](https://github.com/geekingdom/camera-log-record-server/actions/runs/35500093386)已核实全部成功；不能作为本次批量资源新增功能的验收。

R20 独立文件可读短测工具已实现：`scripts/benchmark_file_visibility.py`、`tests/test_file_visibility.py`。真实 Collector 接收至独立进程读取完整行，先读者就绪、最终扫描后关闭，再复核归档摘要。20项专项通过；本机8路×1200行/秒×2秒共19200行完整校验，P99=114.645ms，实验目录已清理。输入限定每read一条完整行；该短测不证明任意分包或500路全天容量。HTTP内容接口、页缓存可见性与fsync持久化分别报告，详见[本轮证据](history/2026-09-20-file-visibility.md)。

本增量后端1668项、前端172项、类型检查/构建及全仓Ruff通过，独立审查无阻断项；不涉及生产UI或采集逻辑变更，未重复浏览器布局验收。新增提交的CI须按对应SHA独立核验，不沿用下方历史绿色结果。

| 当前新增需求（2026-09-20，已完成本机验证） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 根据节点实际服务器资源自动推荐性能参数，管理员可应用后手动调整 | `node/performance_recommendation.py`、节点配置API、性能编辑器 | 后端1648项通过；弱/强/cgroup/未知/过期资源专项、真实节点接口返回推荐；1440/390浏览器验证应用后手调、刷新不覆盖草稿和滚动保存可达；真实隔离日志/下载链路通过 | 已实现。推荐为保守起点，非磁盘跑分或保证吞吐；只填草稿不自动保存，既有策略不被覆盖。新增提交CI按最终SHA核验 |

推荐依据、规则和实验见[推荐验证](history/2026-09-20-node-recommendation.md)及[性能策略说明](node-performance.md)。本机API已更新，推荐读取不改变已保存配置；真实隔离随机库与临时日志已回收。

本增量前端172项、类型检查、生产构建、Ruff与差异检查均通过。

最终修复提交`009d25b`的[CI35497627895](https://github.com/geekingdom/camera-log-record-server/actions/runs/35497627895)八项全部成功，含新增推荐浏览器专项。上表新增功能待核CI已闭环；后续仅记录此结果的文档提交不作为新的源码验收SHA。

提交`5817d51`的CI七项成功，前端失败在旧节点编辑浏览器脚本强制滚到底后检查原本位于末尾的写入延迟字段；新增推荐区后该字段不再是末尾。已本机复现并修正为滚动至目标控件，增加正文上下边界与保存区不遮挡断言；原场景及推荐专项1440/390通过，类型检查/构建通过。推荐专项已加入常规CI，最终结果按修复SHA核验。

前一阶段最终提交`6fe4416`的[CI35496401209](https://github.com/geekingdom/camera-log-record-server/actions/runs/35496401209)八个作业全部成功，包含后端、前端、完整容器、独立组件、原生部署、跨Worker、NFS及提交规范。以下前阶段“后续CI待核验”已由此结果闭环；新增推荐功能须独立验证。

| 当前需求（2026-09-20，已完成本机验证） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 通用写入隔离与有界缓冲，不按单台服务器定制 | `logs/write_io.py`、`storage.py`、`collection/buffering.py`与Collector | 专用执行器不受默认池阻塞；取消等待真实I/O结束；500路×1200行/秒×1秒共600000行逐路摘要与时间前缀一致，缓冲归零；真实API/Worker/WebSocket/小时下载通过 | 已实现专用写盘池、索引句柄复用、4KiB读前预留和事件背压。短测总排空6.370秒，不能宣称持续吞吐或文件可读P99达标；Linux混合长跑待验收 |
| 每节点AUTO/MANUAL性能预算与实际资源识别 | `node/performance.py`、`resource_limits.py`、`performance_runtime.py`、管理API/前端节点控件 | cgroup v1/v2父级限制、未知遥测、冷却恢复、热配置及准入专项；1440/390节点看板和打开编辑弹窗通过 | 已实现。CPU/内存压力可降写并发，纯写延迟只降后台读；不改变管理员容量及延迟阈值，批量/等待按管理员保存值；物理异构节点持续验收待环境 |
| 写入阶段指标与后台争用控制 | `write_io.py`、`compression.py`、`archive_access.py`、节点健康排序 | 压缩摘要/复制/复核统一逻辑计费；取消关闭与提交异常归档计时收尾；缓冲成本排序和归档积压告警回归 | 已实现最近1024样本摘要、归档等待、压力下暂缓索引补建。预算不是全盘物理带宽限制，已提交压缩作业保留提交时预算；直接Coredump下载不受该读取预算统一控制 |

实现边界见[节点性能预算](node-performance.md)，实验与失败原因见[本轮验证记录](history/2026-09-20-node-performance.md)。后端最终全量1642项通过，最后压缩异常路径3项通过；Ruff、前端172项/类型检查/构建、浏览器专项与真实隔离链路通过。模拟目录与隔离随机库已清理，未删除真实日志。本轮不引入消息队列，不以大内存推断磁盘吞吐，不自动扩大持久化风险窗口；新提交CI须按最终SHA单独核验。

本机收尾：确认14项任务全部STOPPED、活动作业为0后启动API/Worker与5173前端，三个健康访问均200；`local-dev`心跳为NORMAL、accepting=true、缓冲0/64MiB，实际写并发4、批量256KiB、最长等待100ms、后台读取50MiB/s。未启动或修改真实设备任务。

集成跟进：性能实现提交`55df81b`已推送。额外复查发现时差验证脚本的稳定遥测缺少新增资源限额，本机真实Mongo复现后补全测试输入，正负8小时时差、CPU高压拒绝和恢复领取全部通过且随机库已清理；没有放宽未知内存生产准入。最终CI按后续修复SHA核验。

`7ece0b9`的CI另暴露故障转移验证器仅调度一次却不等待节点心跳准入就绪的竞态，已补A允许/B禁止的明确等待；真实Mongo/SSH双Worker连续3次通过，峰值连接1且每轮随机库与日志清理。相关12项测试通过。该SHA其余七项CI均成功；新修复提交CI仍须单独核验。

| 当前CI修复（2026-09-18） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| Linux SQLite遇NUL后未索引后续文本，可能漏检 | `logs/search_index.py`候选文本NUL转空格，schema升2；原始日志和边界不改 | 确定性词法截断/分块/旧schema回退测试先失败后通过；修复提交d4a08ea的Linux CI后端1604项及Ruff通过，维护入口重建通过 | 已修复；旧schema自动回退原文并由Worker补建，部署时更新全部Worker |
| 新资源认证成功后立即新建SSH任务被409拒绝 | `resources/api.py`创建成功文档同步初始化ONLINE、检查时间、失败计数和下次认证时间 | 正式API资源创建后立即SSH任务201回归；真实Mongo/SSH双Worker暂停、同run恢复、重新认证及预算验证通过；修复CI的cross-worker-read成功 | 已修复，保持当前认证成功准入门槛；本机已重载且健康200 |

修复提交`d4a08ea`的[CI35299148782](https://github.com/geekingdom/camera-log-record-server/actions/runs/35299148782)八个作业全部成功，包含Linux后端1604项、前端、跨Worker、完整容器、独立组件、原生部署、NFS及提交规范。此处为已完成的代码提交证据，后续纯文档提交的CI不能冒用该SHA。

| 当前需求（2026-09-18） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 新建SSH/Telnet设备任务自动使用资源凭据，并可查看密码 | `TaskEditor.vue`、资源专用credential接口、`tasks/creation.py`事务内复制密文 | 资源凭据prepare后更新仍取事务内新值；普通查询脱敏、所有权校验；桌面/390px浏览器验证密码眼睛、手工编辑/迟到响应隔离 | 已实现。仅资源所有者/管理员且有resources:write可取明文；其他有任务创建权限的用户可服务端复用，不泄露密码；任务保存独立副本 |
| 仅当前认证成功的海康资源可新建SSH/Telnet设备任务，串口不受限 | `tasks/resource_binding.py`、`creation.py`、任务编辑器协议选项 | 非ONLINE/无历史认证拒绝，事务内再校验；串口各异常认证状态仍可创建；离线资源快捷入口保留，已有离线任务仍可编辑 | 已实现；不改变既有有效采集连接在ISAPI异常时继续采集的规则 |
| 未选模板时从任务表单快捷保存初始化与定时命令模板 | 任务编辑器复用已有命令模板创建接口 | 草稿校验、128字名称、私有模板、初始化顺序/定时配置、保存中重复点击和关闭重开隔离；浏览器通过 | 已实现；模板复制后任务仍使用独立配置，新增模板立即可选 |
| 资源与任务表格全部业务列升序/降序/取消排序 | `resources/listing.py`、任务列表API、两类表头及工作区查询状态 | 真实Mongo32资源跨页、双绑定任务去重、16个Coredump组合筛选、创建人回退和派生删除时间验证；浏览器三态排序/移动截图通过 | 已实现。任务数排序按数据库聚合后分页，普通字段先分页再汇总任务；没有以空样本为由新增索引 |
| 大量小时归档精确检索提速并提前显示命中 | `logs/search_archives.py`、`search_results.py`、`search_stream.py`、`LogArchives.vue`与轻量状态接口 | 约128MiB/128分卷开启50MiB/s预算：50.349s→5.226s，128条结果逐项相同；单成员内核首批0.292s→0.040s；物理逆序/取消/水位及真实Mongo20轮发布取消争抢验证 | 已实现顺序扫描与渐进结果；24小时/1000条/8MiB既有上限明确提示，不以部分结果冒充完整结果 |
| 持久化全文候选索引及跨Worker检索 | `logs/search_index.py`、`search_plan.py`、`search_candidates.py`、`search_index_maintenance.py`、节点候选RPC、压缩与保留清理 | 严格600MB原文是已知单设备最大值，非典型；稀疏扫描15.055s→0.505s，另加首次索引查询0.552s；中文/跨分卷/无命中全等价，三小时可跳过无关包；真实Mongo目录64返回/64键/64文档无排序，Unicode随机属性及取消收尾通过 | 已实现。索引仅候选，原文复核；损坏/缺失/短词回退，单索引256MiB上限且不入下载包；物理多节点、多人混合持续负载和TB规模延迟待验收，不能承诺任意范围秒级 |

本轮证据见[2026-09-18验证记录](history/2026-09-18-task-defaults-sorting-search.md)，检索目标和边界见[检索性能合同](log-search-performance.md)。索引集成后后端全量1596项、前端172项/构建通过；最后另补取消收尾与NUL/缓存失效专项测试通过。真实隔离API/Worker/WebSocket/小时下载通过，随机库及合成日志已清理；前端三个专项浏览器脚本通过。上一轮`e8521da`的[CI35222904190](https://github.com/geekingdom/camera-log-record-server/actions/runs/35222904190)成功，本轮CI需按新提交单独核验。

收尾核验：索引末次专项23项、API文档14项通过，隔离浏览器使用已有Playwright运行库重跑成功。确认本机任务运行/暂停意图、节点归属、活动作业均为0后重载API/Worker，API、Worker和5173前端健康均200；未操作真实设备或删除真实日志。

| 当前需求（ISAPI与采集连接独立） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| ISAPI异常但SSH/Telnet设备仍输出日志时继续采集；二者均失效才系统停止，串口不受认证影响 | `resources/health_policy.py`、`health.py`、`collection/liveness.py`、Collector/Runtime/Worker与任务准入 | 真实HTTP 401/503/停止监听均保持同一SSH会话；后续源静默且下一次认证为次日仍自动停止，54行归档与源完全一致；串口三类异常下创建/启动/领取通过；全量1556项通过 | 本机已加载；公司需同时更新API与Worker。HTTP展示保留真实结果，未领取网络任务仍需成功认证；生产应用崩溃与真实网络故障待现场验收 |
| HTTP应用故障期间保留有效连接上的Coredump和资源指标监控 | `collection/coredump_lease.py`、`resource_metrics/runtime.py`、资源监控状态接口与从机引导 | 监控/从机相关回归及最终全量通过；独立复审无阻断；统一单调时钟，真实存储阻塞不算静默，清洁文件不重复fsync重置窗口 | 本机已加载；真实NFS及公司设备故障场景未本轮重演 |

本轮最终验证：后端1556项、Ruff与差异检查通过；真实Mongo认证历史并发聚合和资源停止/恢复事务通过；32路×1200行/秒×3秒共115200行，原文与归档逐路摘要一致、时间前缀正确，合成目录全部回收。隔离HTTP/SSH/Worker验证也已清理随机库与临时日志，未操作实体设备。重载前本机活动任务意图、节点归属和活动作业均为0，重载后API/Worker/前端健康200且local-dev新心跳允许接入。前端本轮仅共享类型注释，沿用本增量已通过的170项与构建，无布局或实时呈现更改。详见[ISAPI独立判定证据](history/2026-09-17-isapi-log-independence.md)；本轮提交CI需按最终SHA另核。

| 当前需求（认证结果合并） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 连续周期认证的全部相同结果合并并累计次数 | `resources/authentication_records.py`、认证接口字段说明与前端共享类型；保留同UTC日/身份/来源边界，message为最近说明 | 成功、凭据失败、离线、异常各16路真实Mongo并发只存1段16次；状态交替3/2/4/3计数守恒、固定TTL、最近消息验证通过；最终联合回归1556项通过 | 本机已加载。历史不批量重写，后续观测可续现有末段；跨日、人工操作、身份变更或迟到观测仍新段，90天保留期不续延 |

前一提交`259b0f2`的[CI35216973925](https://github.com/geekingdom/camera-log-record-server/actions/runs/35216973925)已成功，此结论不替代本轮认证聚合提交的CI。

CI修复（2026-09-17）：`b45ad7c`的[CI35174836312](https://github.com/geekingdom/camera-log-record-server/actions/runs/35174836312)仅backend静态检查失败，1519项后端测试及其余七作业均成功。CI Ruff0.16.8与本机0.16.6存在规则差异；升级本机后复现SIM117，合并`resources/authentication.py`上下文管理器并在`pyproject.toml`固定Ruff0.16.8。修复后全仓Ruff、认证相关57项通过；修复提交`058ad2c`的[CI35178903964](https://github.com/geekingdom/camera-log-record-server/actions/runs/35178903964)八项全部成功，已完成核验。详见[修复证据](history/2026-09-17-ci-ruff-version.md)。

| 当前需求（Coredump提示与事件查询） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 资源存在Coredump时图标红点，并可筛选存在/无文件 | `coredumps/presence.py`、`scanner.py`、`resources/api.py`、`ResourceWorkspace.vue` | 真实Mongo分页前关联、删除源后消除提示及Explain通过；资源页1920/1440/1024/390/320浏览器验证，8px红点不裁剪、打开文件页不清除；相关75项通过 | Worker不可达时为最后已知状态，不承诺即时物理探测；公司环境需更新Worker/API/前端 |
| 每资源每文件版本记录一次Coredump发现事件 | `coredumps/events.py`、`scanner.py`、事件展示器及详情抽屉 | 失败补写、重复扫描去重、保留期删除不复活、附属flag排除、源缺失和FROZEN副本语义测试通过；真实扫描与Cookie页面详情联通 | 按运行事件默认90天保留；旧文件再次被扫描时按原首次发现时间补记，未再可见的历史源不伪造事件 |
| 运行事件与审计记录均按中文事件摘要检索，运行事件增加对象条件 | `administration/api.py`、`event_queries.py`、`event_presenter.py`、`AuditWorkspace.vue`、OpenAPI说明 | 中文摘要、对象名称/IP/ID、旧时间及游标组合、权限测试通过；真实Cookie浏览器验证两类摘要和Coredump对象、桌面移动截图通过 | 保留旧action/type参数；对象关联命中超过1000返回422要求细化，不静默截断 |

本轮全量后端1526项通过后，最后相关75项、前端170项、构建、Ruff和差异检查通过。真实Mongo关联命中既有资源索引，源复核新增(nodeId,id)索引50结果/50键/50文档无排序；隔离真实API/Worker/WebSocket/小时下载通过并清理临时库与文件。本机新API/Worker健康200，四类新增筛选正式接口200。详见[本轮证据与边界](history/2026-09-17-coredump-presence-event-filters.md)。

| 当前需求（2026-09-17） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 跨机Worker与平台时差正负8小时仍按持续心跳判断在线 | `node/worker.py`使用Mongo `$currentDate`写接收时间，节点时间仅存`workerReportedAt`；遥测单调年龄与`health.measurements`校准，API返回平台采样时刻 | 后端全量1518项、最后相关46项通过；真实Mongo随机库验证正负8小时心跳连续、CPU压力拒绝与恢复领取、停止上报31秒离线，验证器`verify_node_heartbeat_clock.py` | 公司节点需更新Worker与API；旧版心跳无法反推接收时间，不伪造历史在线；平台API与Mongo时间基准仍需一致，未放宽业务租约 |
| Docker节点网络数据与MobaXterm的Mb/s对照 | `node/telemetry.py`返回实际网卡/采样窗口/统计口径；`NodeList.vue`上下载显示Mb/s并悬浮等效字节值，采集输入保留字节；收紧lo过滤避免误排long0 | 2秒接收2500000字节=1250000 B/s=10Mb/s回归；前端167项、构建及1440/390看板与悬浮浏览器验证通过 | 公司现场偏差根因未证实：需同一时段、同一网卡对照；当前源码宿主proc与排除容器重复流量口径已核对，不能把单位差异当作全部根因 |

本轮收尾：本机API、Worker健康接口和5173前端均为200，正式节点接口`local-dev`为HEALTHY并返回校准遥测及网卡/采样窗口；独立审查无阻断项。完整证据见[心跳与网络核验](history/2026-09-17-heartbeat-clock-and-network.md)。公司网络差异和物理跨机验收仍按上表保留。

| 当前需求（2026-09-16，已完成本机验证） | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 异常节点上的运行任务自动在其他合格节点重新采集，旧日志不迁移 | `node/failover.py`、`tasks/scheduler.py`、`tasks/claim.py`、Worker内部关闭接口；精确旧运行关闭证明后创建新运行，保留用户暂停/停止意图，排除故障原节点；数据库恢复按专属状态重试未完成关闭 | 真实Mongo、回环SSH、双Worker确定性生产函数编排通过：新run、旧锁释放、全程峰值1连接、两侧独立READY归档；全量1512及末次相关86项通过，临时库和日志已清理；已接入CI | 完全失联且无可信关闭/隔离证明仍等待隔离；当前针对心跳/数据库失联，非任意健康预警迁移。自然后台时序、物理网络分区和目标Linux多机仍待验收，见[当前证据](history/2026-09-16-node-failover-live-budget.md) |
| 实时窗口取消主动高频省略，按可配置预算保留最新日志 | `liveLogBuffer.ts`、`LiveLogs.vue`、`collection/runtime.py`；默认10MiB、1至100MiB内容与索引估算预算，超限淘汰最旧内容；暂停/查找时预算缩小也裁剪快照 | 1200行/秒合成缓冲测试；桌面/移动端终端及最旧范围补读浏览器通过；真实隔离API/Worker/WebSocket/小时下载通过并清理临时库和日志；前端166项、构建及Ruff通过 | 内容预算不等同浏览器RSS；网络缺口仍需显式补读，首次打开从服务端最近帧开始；目标持续高频网络吞吐及全天容量未验收 |
| 后台可保存显示预算，配置改为横向模块导航 | `administration/settings.py`、`SettingsManager.vue`、`settings/api.ts`；平台与容量、节点准入、资源监控、记录保留四模块；普通用户只读最小显示配置，现有页面每30秒更新 | 1440/390浏览器验证保存、模块切换及保留草稿，截图检查通过；配置/API文档/请求对象26项通过；本机API/Worker已更新且健康，新接口真实返回200与默认10MiB | 目标服务器需部署同版API、Worker和前端；设计见[node-failover-and-live-window.md](node-failover-and-live-window.md)，本机证据与外部验收边界已分开 |

| 当前请求记录问题 | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 请求对象统一显示未记录 | `common/request_targets.py`、`observability.py`、`administration/event_presenter.py`、前端审计表格和详情 | 保存路由声明的对象ID并批量补名称；集合/平台操作明确范围。对象及请求专项21项、前端164项、构建、真实Cookie三类审计页面及1440/390截图通过；后端全量1500项通过 | 旧记录未保存的实体ID无法恢复，集合类型可在读取时补齐；30天TTL和请求频率不变，见[当前证据](history/2026-09-15-request-target-and-client-ip.md) |
| 公司来源IP显示Docker容器地址 | 用户使用`deploy-all.sh`跨机完整部署，脚本内部拆分四个组件项目但共用入口`.env`，不是用户独立部署；部署说明已更正，`tests/test_proxy_headers.py`覆盖策略和请求审计 | 用户实测API的`FORWARDED_ALLOW_IPS=127.0.0.1`，不信任实际对端172.21.0.2；可信代理恢复浏览器10.41.203.12、非可信XFF仍拒绝的联合回归通过；2026-09-16核实`deploy.sh`跨机分支逐项传递同一env_file | 在原统一`.env`加入代理172.21.0.2后仅重建API，Compose的DEPLOY_ENV_FILE与--env-file均须指向该文件；不得另建.env.backend。未远程执行，目标新请求待验证，历史误记IP不可可靠回填 |

| 当前问题 | 实现位置 | 验证证据 | 未完成与下一步 |
| --- | --- | --- | --- |
| 跨机 Worker 在登记页在线、节点看板离线 | `administration/settings.py`、`node/health.py`统一30秒窗口并区分未来心跳；`commands/api.py`返回`assessedAt`；`nodeDashboard.ts`、`useWorkspaceCollections.ts`以服务器时间与浏览器单调计时更新快照 | 22项节点/设置回归、前端162项、生产构建、1440/390看板截图及浏览器快8小时验证通过；隔离真实API/Worker/WebSocket/小时下载通过并清理；见[心跳一致性证据](history/2026-09-15-node-heartbeat-consistency.md) | 公司目标机器的实际心跳和时钟尚未提供，不能认定其根因已证实；更新API/前端后核对`heartbeat`与`assessedAt`及A/B服务器UTC时间，真实时钟偏差须同步时间，不能放宽租约或伪造在线 |

上表2026-09-15的时钟排障要求已由2026-09-17新需求更新：升级后的Worker用Mongo接收时刻上报心跳，Worker自身正负8小时时差不影响在线；原时钟同步要求仅保留于平台API与数据库时间基准，以及日志时间等独立业务。旧版Worker需升级，不按8小时延长失联窗口。

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
| R17 实时虚拟列表/容量保留、ANSI、暂停跟随与续传 | `LiveLogs.vue`、`LiveLogRanges.vue`、`logs/gap_catalog.py`、`gap_snapshots.py`、`shared/composables/liveLogBuffer.ts`；取消200行/秒及5000行主动省略，改为管理员配置1至100MiB、默认10MiB的最新内容预算；跨文件/会话补读保留 | 高频1200行/秒合成输入；1440/390/320补读浏览器验证最旧50行淘汰、最新200行保留和暂停时预算收缩；已有精确五页补读与`DELETED`缺口分页证据保留 | 配置值估算日志内容和索引，不等同浏览器RSS；无file/offset或目录不完整的源缺口只能转小时归档；物理跨节点与真实持续采集规模未验收 | 目标多节点部署后验证目录一致性和真实流；不再恢复已取消的按速率主动省略 |
| R18 保留天数、节点登记/准入、审计/事件 | `administration/settings.py`的保留期/节点配置与审计同事务，`node/input_admission.py`，`record_maintenance.py`，事件展示/筛选；已实现 | 节点输入速率真实Mongo竞争/恢复、设置保存、增长记录维护和20项生产页面验证通过；事件既有真实Mongo派生查询与浏览器证据保留 | 登记不等于部署；目标节点持续输入和生产事件/索引成本未验收 | 部署同版API/Worker/前端后，按目标吞吐标定阈值并观测维护与事件查询成本 |
| R19 Token/撤销/权限、加密审计、TLS | Token/公共鉴权、`users/sessions.py`；有效用户共享读取，写入按所有者，令牌继承绑定用户，旧独立资源/任务范围模型已替换 | 权限/脱敏/会话及服务令牌测试，本轮全量通过 | 分布式 TLS 未验收 | 真实代理与会话撤销验证，参见R34/R35 |
| R20 500×1200 行/秒×24小时，文件可读 P99≤200ms | 压测工具、写入指标及`benchmark_file_visibility.py`独立文件短测，整体待验收 | 20项探针专项；8路×1200×2秒逐行/归档摘要通过，P99=114.645ms；[证据](history/2026-09-20-file-visibility.md) | 无等规模持续证据；短测限定一行一read、页缓存可见；API不能替代文件观测 | 在Linux集群扩展有界持续采样并完成混合负载全天验收 |
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
| `scripts/benchmark_file_visibility.py` | Collector接收至独立进程读到完整行，逐行最近秩分位；含5ms目录轮询及观察调度 | 本机原始文件页缓存可见性；有限模拟输入，不是500路全天或fsync证据 |
| API 直方图溢出 | 200ms 以上使用观测最大值给保守 P99 上界 | 非精确 P99，超标不能单独证明原指标失败 |

显式 `httpx.Limits(max_connections=...)` 的 `max_keepalive_connections` 为 `None`，不能套用默认构造的 20。依据依赖参数作判断前必须核对实际构造和安装版本。历史 64 路实验未通过 API 延迟与并发窗口，不能宣称容量达标。

## 后续验收

以下为持续目标尚未完成的验收，不代表已经交付。每轮先核对最新业务指令、本表、Git最新提交及工作区，再选择可执行的下一项；重新核实服务、任务和测试产物，禁止将本表的历史时点当成当前实时状态。旧的暂停指针和历史CI提交不再作为当前流程入口。

1. 目标服务器部署及真实TLS证书/代理、多机节点连通性验收；本机API/Worker已更新，34及35实机暂停恢复已验证，35新增真实空闲重连证据；专用测试任务已停止并清理日志。
2. R11跨节点物理隔离；R26日志作业已具备持久租约和明确失败收尾，后续补故障产物关闭证据与受限回收、真实大文件故障矩阵；已完成的数据库事务不重做。
3. R20 Linux集群500路全天容量、持续独立文件可读P99和压缩率报告；本机独立文件短测已完成，不重复开发该工具。
4. R21后续新建实验产物按来源和到期证据受限清理；本机历史数据已在本次明确授权的开发环境重建中删除。

旧阶段提交、测试和进程记录见[历史验证记录](history/2026-09-09-status-legacy-validation.md)，不得当作当前进程状态。
