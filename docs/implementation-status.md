# 当前状态总表

核对日期：2026-09-09。功能提交 `f80ee14` 已推送，审计排障、五类部署入口及任务创建/控制事务通过 [Linux CI 34324896916](https://github.com/geekingdom/camera-log-record-server/actions/runs/34324896916) 的五个作业；后端 689 项、前端 49 项通过。用 `git log -1 -- docs/implementation-status.md` 定位总表版本。[历史记录](history/README.md) 不替代本表。

## 当前任务与恢复

最新业务重点：日志工作台切换页签的横移与设备资源行快捷新建任务。已增加页面稳定滚动槽和内容宽度约束；资源行按tasks:create及资源范围显示快捷入口，复用任务编辑器并预填资源/IP/协议，保存后刷新资源列表。1440/390浏览器模拟提交与权限检查通过，工作区坐标宽度在各页签保持一致，折叠侧栏后也通过。目标服务器的原始横移未在本机复现，需部署后确认其浏览器效果。见[本轮记录](history/2026-09-09-workspace-and-job-completion.md)。

同期R26：浏览器下载授权提交56a3bbe已通过CI34344296389全部六作业。作业终态新增`logs/job_completion.py`，状态与审计原子提交，失败重试只操作数据库、不重做文件或删除未确认产物；成功/失败/取消/过期的真实副本集回滚、确认丢失和十次取消竞争通过。后端全量746项、前端52项与构建通过。最新新增CI待收取；进程在终态提交前崩溃的持久恢复仍需完善，本机Worker未更新。

当前增量：日志下载/检索作业的文件保护、目录快照、幂等映射、入队和审计已改为同一事务，取消也与审计原子提交。功能提交 `1226e5a` 已通过 [Linux CI 34343287577](https://github.com/geekingdom/camera-log-record-server/actions/runs/34343287577) 六个作业，后端741项及Ruff通过，覆盖原生、独立组件和容器集成验证。真实副本集失败/取消回滚、并发重试、提交确认丢失验证通过，临时数据已删除。本机API/Worker尚未更新。此项推进R26，不替代下述用户最新节点与时区需求；记录见 [日志作业审计](history/2026-09-09-log-job-audit.md)。

最新业务重点：修复服务器 Docker 节点 HTTP 登记失败，支持删除节点，并解释 UTC 服务器与上海小时归档的时差。功能提交 `dfe70a4` 已通过 [CI34341798389](https://github.com/geekingdom/camera-log-record-server/actions/runs/34341798389) 六个作业，含真实 Docker 默认 HTTP 节点登记/保存/持续心跳、节点删除竞争、原生与独立部署及多路分卷归档。本地后端736项、前端52项/构建、真实副本集及模拟浏览器12次确认操作通过。未连接用户服务器，未更新本机API/Worker；下一步按部署排障文档更新目标服务，继续全项目剩余验收。见 R29/R30 和 [本轮记录](history/2026-09-09-node-management-and-timezone.md)。

前一增量 `2607354` 的 [CI34339927183](https://github.com/geekingdom/camera-log-record-server/actions/runs/34339927183) 六个作业已全部成功，含真实手动命令审计事务验证。服务 Token 创建/撤销审计事务本轮已补齐，见 R26，不能继续列为未实现。

已完成的访问兼容要求：Docker和原生部署均支持普通HTTP服务器IP操作。`eb4e829`共用UUID修复已通过[Linux CI34338375626](https://github.com/geekingdom/camera-log-record-server/actions/runs/34338375626)六个作业。`443c336`将真实非loopback IPv4 Chrome验证纳入CI，[CI34338617932](https://github.com/geekingdom/camera-log-record-server/actions/runs/34338617932)也全部成功。目标服务器仍需拉取并重新构建前端，不能以历史404问题替代最新节点需求。

最新用户问题复核（2026-09-09 15:25 上海时间，工作区仍以 `59337d8` 为提交基线）：旧 API PID 49854 的 OpenAPI 中没有 `/api/v1/request-events`，访问日志在 `.local/service-logs/api/camera-logs.jsonl:2318` 记录请求 `6dd61839ad6b48f193c760193dbe5199` 于 15:25:30 返回 404、耗时 2.297ms。受控更新 API 至 PID 79958 后，审计、运行和请求三类事件接口均以管理员 Token 返回 200；Chrome 刷新请求记录后 404 消失并正常显示空列表。未导入旧文件访问日志，未改动真实业务数据。Worker PID 25871 未重启，34/35 的 `COLLECTING` 状态、run、session 和 generation=28 均保持不变。

部署状态：Ubuntu/Debian原生部署入口已实现并通过Ubuntu24.04验收，具体边界见R27。上一增量任务编辑与登录/退出/改密事务`175b938`已通过Linux CI34328314712；真实副本集验证结束均清理临时库与日志。本机API更新至PID98392，三事件接口200；Worker PID25871未重启，两路运行、会话及generation=28不变，更新窗口日志分别增加4530/14335字节。Worker错误收尾小修尚未加载本机。后续继续目标服务器产品验收、命令/作业审计一致性与集群容量验收；白名单只约束平台客户端，不约束设备目标；生成交接摘要不是业务目标。

恢复时先读最新业务消息与总表，再核对工作区、提交和相关源码。摘要中的默认值、运行状态和结果必须重新验证。“源码/测试存在”不等于本轮重跑通过；继承的历史结果明确标为历史。

用户后续修改优先：资源优先、同端口可多任务、SSH 不默认拦截指纹、日志行添加服务器时间、10 MiB 有序分卷、小时包仅含日志、资源软删除保留日志。

最新配置变更（2026-09-09）：已按用户最后一次更正轮换 34、35 的本地 debug 模拟挑战码，串口 10003 同步映射 34。通过实际 `PshPasswordProvider` 的 mock 读取校验三项，配置在 Git 忽略的 `.local/secrets/psh-passwords.json`，不记录明文、不触发实体设备 debug。提供器逐次读取，下一次命令使用新值，无需重启采集。

当前新增业务：Linux 一键部署、用户名登录/内置管理员/子账户资源与功能权限、平台客户端 IP 多规则权限。默认管理员密码按用户明确要求为 `asdf!234`，首次强制改密；IP 规则不适用于任何设备资源或采集目标地址。功能提交 `2c62fdb` 已通过 [Linux CI 34304331562](https://github.com/geekingdom/camera-log-record-server/actions/runs/34304331562)，不以此替代集群全天容量验收。

## 需求对照

后端简写路径均相对 `backend/camera_logs/`，前端组件位于 `frontend/src/features/`。局部验证通过不等于整体交付。

| ID / 最终需求 | 实现位置与状态 | 验证证据 | 未完成部分 | 下一步 |
| --- | --- | --- | --- | --- |
| R01 Python/Vue3/Mongo，控制台与第三方共用 API | `main.py`、前端、`pyproject.toml`，已实现 | 源码；历史 CI 34296982222 | 多节点部署未验收 | 部署复核 |
| R02 海康 Digest/Basic 认证，解析型号、短序列号、版本 | `resources/authentication.py`、`resources/api.py`，已实现 | `tests/test_resources.py` | 本轮未重测实体认证 | 401、异常、XML 分支验收 |
| R03 串口资源、多任务、协议/IP 限制，串口可选服务器或自填 | `tasks/resource_binding.py`、资源前端，已实现 | `tests/test_task_resources.py` | 全流程设备验收需补充 | 资源→任务验收 |
| R04 相同设备身份同父目录；软删除保留日志 | `tasks/resource_binding.py`、`resources/lifecycle.py`、`logs/storage.py`，已实现 | 资源/存储测试 | 跨节点仅相对路径一致，非共享物理盘 | 跨节点目录验证 |
| R05 表单、初始化排序、正整数定时参数、IME、密码保留 | `common/models.py`、`TaskEditor.vue`、`CommandEditor.vue`，已实现 | 模型/表单测试，历史浏览器冒烟 | 二次确认见 R16 | 统一交互验收 |
| R06 模板 CRUD/版本/独立副本与计数 | 模板模块、命令编辑器，已实现 | 模板/任务测试 | 管理全链路需复测 | 替换与删除后快照验证 |
| R07 逐路隔离、有序写入、重复正文保留 | `collection/collector.py`、`collection/runtime.py`、`logs/storage.py`，部分验收 | 存储/故障测试，短时摘要报告 | 500 路全天未证明；SSH PTY 不证明跨独立流时序 | 独立源序号验收 |
| R08 SSH 按凭据连接、不以变化指纹拦截、保活及十秒空闲重连 | `collection/connections.py`、`collector.py`、`runtime.py`，已实现 | 既有连接测试；本轮 34/35 恢复后各一条新 Worker SSH 连接，日志继续增长 | 实体空闲故障与集群迁移未验收 | 继续故障与集群验收 |
| R09 初始化/定时队列、断线续计、重启归零、发送预算 | `commands/reservation.py`、`collection/runtime.py`，数据库原子性已实现 | 本轮重读事务源码；祖先提交 `92e01d6`；真实 Mongo 验证脚本 | socket 不属于数据库事务，设备执行结果仍可未知 | 保持 UNKNOWN、不补发；勿重复实现预算事务 |
| R10 手动优先、不跨会话、断线拒绝与审计 | `commands/manual_submission.py` 将入队、幂等映射和审计同事务提交；原准入/领取已部署，新增提交入口尚未加载本机 API | 本轮真实副本集证明审计失败/取消整体回滚、同键并发返回同一命令、提交确认丢失后只读恢复、停止后同键重放；临时数据已清理 | 最终 DB 检查至 socket 写入仍需物理隔离；历史 PENDING 仅重放，不补造审计 | 部署新增 API，继续 R11 接管隔离 |
| R11 幂等启停、受控重启、租约/代次隔离 | `tasks/editing.py` 编辑、资源声明、停止操作、审计同事务，安全排队编辑保留RUNNING；Worker已知失败保持STOPPED | `175b938` Linux CI 34328314712通过；真实副本集回滚/竞争/确认丢失与排队调度验证；API已更新 | 跨节点物理隔离未证明；Worker错误收尾小修尚未加载本机 | 继续旧实例接管隔离，Worker部署需受控维护 |
| R12 PSH 密文、ls 探测、模拟口令、失败仅影响当次 | `collection/psh_*.py`、`collector.py`，已部署本机 | 既有恢复及预算测试；本轮普通初始化和手动发送恢复成功，未发送 debug | 设备仍处于 Password 时不能发送业务命令；真实解密接口与 10003 切换未验证 | 在具备有效挑战码与接口条件后专门验证，不反复试错 |
| R13 10 MiB 编号分卷、上海小时、仅日志 tar.gz、归档后删原卷 | `logs/storage.py`、`logs/compression.py`，已实现 | 本轮核对校验→发布→同步→unlink；存储测试 | 集群验收未完成；10M 当前按 10 MiB | 保持校验失败保留原卷 |
| R14 小时查询、统一小时包、多选 ZIP、Range | `logs/hour_download.py`、`logs/export_output.py`、日志前端，已实现 | 下载/归档测试、历史浏览器下载 | 分布式缺片与规模限制待验收 | 多小时端到端校验 |
| R15 流式搜索、并发/读预算、配额与到期清理 | `logs/jobs.py`、限制器、`logs/maintenance.py`，部分验收 | 导出限制测试、合成报告 | 混合持续负载未证明 | 与采集联合验收 |
| R16 美观 UI、侧栏折叠/滚动、状态按钮、修改二次确认 | 前端 app/features，命令草稿删除已补确认；按对象定位避免异步确认误删 | 前端 32 项测试；`browser_confirmations.mjs` 11 项模拟写入、零控制台错误；1440/390 截图核对 | 真实设备完整产品链路尚需专用模拟任务复验 | 保持草稿确认与保存确认独立，推进全链路验收 |
| R17 实时虚拟列表/限速、ANSI、暂停跟随与续传 | `LiveLogs.vue`、`LiveLogRanges.vue`、`shared/composables/liveLogBuffer.ts`；已知字节范围补读已实现 | 前端 42 项；浏览器精确五页补读、暂停/任务切换/迟到响应及 1440/390/320 视口；隔离真实 API 的 1200 行/秒模拟流补读与全链路通过 | 服务端无 file/offset 的 gap 事件只能转小时归档；仅保留最近更新的 200 个范围；跨文件范围分别阅读 | 后续完善跨节点/跨文件未知缺口目录定位，继续规模验收；不重复实现已知范围窗口 |
| R18 保留天数、节点登记/准入、审计/事件 | `administration/settings.py` 的保留期/节点配置与审计同事务；`event_presenter.py`、`event_queries.py` 和审计界面提供中文摘要、关联对象、安全原因及 Mongo 派生筛选分页 | 隔离 Cookie 浏览器显示连接缺口、任务/IP、级别/结果与详情；真实 Mongo 派生查询验证与 Linux CI 34324896916 通过；本机三事件接口 200 | 登记不等于部署；缺输入速率准入阈值 | 继续配置生效与速率准入验收 |
| R19 Token/撤销/权限、加密审计、TLS | Token/公共鉴权、`users/sessions.py`，会话用户支持资源范围；服务 Token 保留任务范围 | `test_user_permissions.py`、权限/脱敏测试，本轮全量通过 | 分布式 TLS 未验收 | 真实代理与会话撤销验证 |
| R20 500×1200 行/秒×24小时，文件可读 P99≤200ms | 压测工具与写入指标，待验收 | 短时报告仅证明对应样本 | 无等规模证据，API 观察不能替代文件可读 | 独立文件探针与 Linux 集群全天验收 |
| R21 及时清理开发日志，保留证据 | 下载副本清理及新 `scripts/cleanup_dev_server_logs.py`、`dev_cleanup_evidence.py`；受限 ID 维护入口 | 历史清理 1,028 个副本/601,373,134 字节；本轮实际解压核验 10 归档/18 catalog/3,080,799 字节，预览 PROTECTED | 两份合成数据最晚保护到上海 2026-09-09 23:12:23.673；当前未物理删除；失败实验仍需完整来源证据 | 到期重跑预览/执行，不缩短保护或全局保留期；见开发清理文档 |
| R22 中文提交、部署/API/运维文档、持续总表 | `AGENTS.md`、hooks、CI、docs，部分交付 | 本轮总表和历史入口，既有提交校验 | 压缩率与全天容量报告未交付 | 每次同步对应状态行 |
| R23 五类 Linux 部署、中文配置、自启动、重复执行保留配置与卷 | 根部署入口、`deploy/*.yml`、`mongo-host-user-init.sh`、`deploy_component.py`；组件后缀隔离项目；systemd 启用 Docker、常驻容器 `unless-stopped` | 本地部署回归 43 项；Linux CI 34324896916 实际完整部署两次及独立四组件部署/重跑/重启；返回 restartHealth、temporaryProjectsRemoved、temporaryDataRemoved 均 true | 未实际重启宿主机；裸机安装 Docker 和 systemd 启用分支为脚本检查；未验收其他发行版与多机部署 | 在目标服务器验收开机启动及真实多机网络；维护已有环境和数据 |
| R24 正常登录、内置管理员、子账户及权限 | `users/`、前端 `features/auth/`、`app/AppNavigation.vue`；App 已拆至 494 行 | 前轮 Linux CI；本轮前端 28 项测试含退出后迟到响应/恢复失败清理，构建和模拟浏览器通过 | 真实浏览器完整采集操作仍需专用模拟任务复验 | 按产品缺口推进，保持会话代次隔离 |
| R25 平台来源 IP 白名单、多网段独立权限 | `access_policy/`、Nginx、前端 IP 管理，已实现并通过 Linux CI | IPv4/IPv6、权限交集、设备和串口目标不受限测试；CI 真实代理启用/关闭策略及伪造 XFF 检查通过 | 额外反向代理拓扑需单独配置可信来源链 | 部署时按实际代理链检查 clientIp |
| R26 平台访问记录、业务审计与凭据脱敏 | 任务/会话/手动命令/令牌及作业提交取消事务已交付；授权事务已通过CI；`logs/job_completion.py`补齐终态审计与重试 | 授权CI34344296389全部成功；本轮后端746项、真实副本集四类终态回滚/确认丢失/取消竞争通过 | 进程崩溃遗留RUNNING作业的持久恢复未实现；新增终态CI待收取；本机Worker未更新 | 完善作业崩溃恢复，维护产物保留与审计一致性 |
| R27 Ubuntu/Debian无Docker主机部署、五类入口、详细注释与自启动 | `deploy-native*.sh`、`scripts/native_*.py`、`deploy_native.py`、`docs/native-deployment.md`，已交付；Nginx权限和跨组件合同预检已修正 | `eb4e829` CI34338375626 Ubuntu24.04真实首次/重跑/重启通过，配置不变，临时安装已删除 | 未物理重启宿主机；Ubuntu22.04/Debian12自动依赖分支尚未实机验收 | 目标服务器按文档部署，验收实际依赖源和开机启动 |
| R28 其他电脑通过HTTP服务器IP正常操作 | 共用`frontend/src/shared/api.ts`兼容UUIDv4；Docker/原生前端均重新编译该源码；CI加入`verify_http_browser.mjs` | 先复现相同异常，前端52项/构建通过；真实非安全IPv4 Chrome成功发出启动/命令/下载模拟请求；CI34338375626两类部署通过 | 目标服务器需拉取并重新构建前端；模拟浏览器不等于全部实体设备业务验收 | 更新目标服务器前端并强制刷新，继续产品全链路验收 |

## 验收口径

新增需求对照：

| ID / 最终需求 | 实现位置与状态 | 验证证据 | 未完成部分 | 下一步 |
| --- | --- | --- | --- | --- |
| R29 内网HTTP节点登记保存、节点删除、完整/独立部署配置一致 | `administration/settings.py`、`node_lifecycle.py`、`tasks/claim.py`、设置前端、`deploy_env.py`；已实现并推送 | CI34341798389六作业通过，真实Docker节点登记与持续心跳；真实删除竞争/历史保留/迟到心跳保护；浏览器确认及1440/390截图 | 用户服务器尚未更新，本机API/Worker尚未更新 | 按节点部署排障文档更新原服务，不改已有节点身份或日志目录 |
| R30 明确服务器UTC与上海小时归档的时间关系 | `LogArchives.vue` 标注北京时间UTC+8；原后端归档时区保持上海 | 2026-09-09 18:30:12 UTC归入2026-09-10 02点单测；浏览器同日小时样例显示通过 | 不变更服务器系统时区；不是自动跟随主机时区 | 保持日志前缀、小时目录和显示一致 |
| R31 日志工作台切换不横移、资源行快捷建任务 | `styles.css`稳定滚动槽、`LogsWorkspace.vue`宽度约束、`ResourceWorkspace.vue`与App复用任务编辑器；生产页面验证已加入CI | 本机生产构建1440/390页签坐标/宽度、滚动条压力、折叠侧栏、两类资源模拟创建和权限/资源范围隐藏通过 | 新增Linux生产浏览器CI待收取；本机未复现目标浏览器原始横移；目标服务器需更新前端 | 收取Linux截图和结果，部署后复核目标浏览器常驻滚动条设置与切换效果 |

| 指标 | 实际测量 | 与原要求关系 |
| --- | --- | --- |
| 原要求文件可读 P99 | 采集端接收到独立读者可读，需明确按行/批权重 | 尚无 500 路全天证据 |
| `common/write_metrics.py` 的 WriteLatency | 首块接收至批量写入完成，批次数加权、滚动秒桶 | 不独立读文件，不含目录/API |
| `scripts/service_benchmark_latency.py` 的 BatchLatency | 源端发送至正式 API 读齐整批，行数加权，含轮询/排队 | 附加端到端指标，不是原文件指标 |
| API 直方图溢出 | 200ms 以上使用观测最大值给保守 P99 上界 | 非精确 P99，超标不能单独证明原指标失败 |

显式 `httpx.Limits(max_connections=...)` 的 `max_keepalive_connections` 为 `None`，不能套用默认构造的 20。依据依赖参数作判断前必须核对实际构造和安装版本。起始工作区已有 `sniffio` 依赖改动；历史 64 路实验仍未通过 API 延迟与并发窗口，不能宣称容量达标。

## 下一步顺序

最终功能提交 `2c62fdb`：Linux CI 后端 567 项、前端 25 项测试通过，Ruff、前端构建、构建上下文检查及 4 项副本集初始化检查通过；一键部署、真实代理账户权限与多路归档链路通过。具体证据见 [账户与部署记录](history/2026-09-09-access-and-deployment.md)。清理历史见 [清理记录](history/2026-09-09-status-and-cleanup.md)。未重跑的实体设备/集群验证不计入本轮通过项。

当前命令恢复与模块化提交 `d157cc2`：本地后端 573 项、前端 28 项测试、构建、Ruff 与模拟浏览器通过；默认十秒重连/实际暂停补强后再次 2 项通过。[Linux CI 34306652015](https://github.com/geekingdom/camera-log-record-server/actions/runs/34306652015) 四个作业全部成功。历史及真实 worker 尚未重启的部署边界见 [本轮记录](history/2026-09-09-command-recovery-and-modularity.md)。

1. R21：受限服务端清理器已实现；保护到期后重新核验并清理两份合成报告归档。已完成的 R23/R24/R25 不再列为待实现。
2. R16/R17/R12：已知实时范围与独立补读窗口、隔离模拟全链路已验证；继续无边界缺口定位和实体 debug 恢复，不重复开发范围阅读器。
3. R10/R11/R26：手动准入和领取事务已部署本机并完成两路实机发送验证，继续操作审计一致性和接管隔离；不重复开发已完成预算事务。
4. R20：对齐指标后安排集群全天验收，性能诊断与产品验收同步推进。

本轮访问边界与确认交互的验证和限制见 [访问审计与确认记录](history/2026-09-09-access-audit-and-confirmations.md)。新增平台 IP 规则不进入设备认证、采集连接或调度目标校验；受限客户端操作仍须具备对应账户权限。

功能提交 `7bd1fc0` 已通过 [Linux CI 34308408063](https://github.com/geekingdom/camera-log-record-server/actions/runs/34308408063) 的后端、前端、中文提交和容器集成四个作业；本地 577 项后端测试、32 项前端测试及最终相关 22 项回归通过。此证据覆盖 R16/R25/R26 本轮改动，不代表剩余项目验收完成。

R21 本轮后端全量 592 项通过，随后新增脚本集成 6 项通过；最终清理相关 34 项、前端 32 项及构建、Ruff 通过。两份本机报告的预览和执行入口均实际返回 PROTECTED，没有删除受保护归档。证据与重试条件见 [开发服务端清理记录](history/2026-09-09-development-archive-cleanup.md)。

R10 手动命令实现阶段后端全量 610 项、前端 32 项及构建、Ruff 通过；真实副本集并发准入、停止冲突、取消回滚和未知提交验证通过。新增模块分别为 39/77 行，runtime 469 行、collector 492 行。历史测试见 [手动命令事务记录](history/2026-09-09-manual-command-transactions.md)。当前真实 Worker 已更新至代码基线 `7b4964e`，两路恢复与手动发送及 R25 本轮 10 项回归证据见 [本机部署核对](history/2026-09-09-local-rollout-and-ip-boundary.md)。

R17 功能提交 `17747f9` 本地后端 610 项、前端 42 项、生产构建与 Ruff 通过，独立复核未发现阻塞问题；[Linux CI 34314414779](https://github.com/geekingdom/camera-log-record-server/actions/runs/34314414779) 四个作业全部成功。详细边界、浏览器与真实 API 证据及临时数据清理见 [实时范围补读记录](history/2026-09-09-live-range-recovery.md)。

R18/R26 功能提交 `19bdb74` 本地后端全量 623 项、前端 47 项、生产构建、Ruff 和 diff 检查通过；[Linux CI 34316605024](https://github.com/geekingdom/camera-log-record-server/actions/runs/34316605024) 后端、前端、中文提交、容器集成四个作业全部成功，包含新增真实审计事务和多路归档回归。隔离真实 Cookie 浏览器通过，临时服务/数据库/日志目录已回收。独立审查发现的夹具适配遗漏已修正，同名冲突回滚由真实数据库证明。本机 API 已更新为 PID 43955，Worker 25871 未变；34/35 维持原运行/会话/generation=28，日志继续增长。真实用户浏览器已刷新，当前需重新登录；未改真实管理员密码。详情见 [审计会话与事务记录](history/2026-09-09-audit-session-and-transactions.md)。

管理配置功能提交 `87a49da` 本地后端全量 627 项、前端 47 项、构建、Ruff 与 diff 检查通过；[Linux CI 34318069115](https://github.com/geekingdom/camera-log-record-server/actions/runs/34318069115) 四个作业全部成功，含新增正式管理 API 的真实事务及来源切换检查。验证覆盖冷启动默认记录回滚、节点登记/修改回滚、并发 CAS 唯一成功、IP 匹配集合失败不变化/成功后切换。临时数据库和目录均清理，额外核验并删除验证器早期错误派生的 18,846 字节服务日志。本机 API 已更新为 PID 49854，管理查询均为 200；Worker 25871 和 34/35 原运行/会话/generation=28 不变，日志继续增长。下一步为任务控制意图与审计事务，其余项目缺口继续保留。范围与后续见 [管理配置事务记录](history/2026-09-09-admin-audit-transactions.md)。
