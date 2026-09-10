# 当前状态总表

本轮（2026-09-10）交付 coredump/NFS、浅层日志目录、资源周期认证及暂停后恢复的源码阶段。NFS 部署固定允许所有来源（`*`），Docker 与原生部署使用各 Worker 的 `NFS_ROOT/NFS_SERVER_IP`；无效的平台 `nfsRoot` 设置接口已移除，防止保存成功被误认为主机导出生效。隔离Ubuntu真实内核NFS安装、设备IP子目录直挂及双路传输已通过；35真实设备重启恢复与Docker NFS core接收下载已完成有限实测，目标生产服务器、自动每分钟重挂及长期容量仍待验收。

型号/序列号允许缺失或为空；身份将缺失、null、空白统一为空字符串，目录以 unknown 展示。目录组件最多80字节，截断展示部分后追加摘要。同IP且两项身份均空时无法识别物理设备更换。浅层目录已实现；coredump文件接收目录、查询及导出已接入，真实NFS接收容量尚未验证。

测试环境纠正：项目 .venv 已安装 pytest，前几轮仅使用系统 Python 的失败不能作为无法测试的依据。新增目录测试原本有错误预期，运行后已修正并完成定向回归。

最新主代理后端全量957 passed（100.00秒），包含第三方core权限、Telnet重连和分包前缀新增回归；Ruff全量通过。前端85项及生产构建沿用本轮此前结果，后续未修改前端。R51已在35真实reboot中验证OFFLINE后自动恢复COLLECTING，新运行/会话、合法多任务socket和专用任务收尾均通过。35真实core经本机Docker NFSv3接收，首轮页面稳定状态/隐藏flag/导出及并发下载通过；第二轮默认10秒扫描预先接入后发现增长中的新core，完整下载和64KiB Range源摘要一致。该单设备约6.73MB实测不替代长时间大文件容量验收。

核对日期：2026-09-10；最新已核实远端验收基线 `19e5041`，CI `34462650272`七作业成功（含原生部署、Docker部署、NFS及前端浏览器）；此前`aa86ce6`七作业也已通过，主代理收取并查看其桌面工作台和390宽普通用户服务账号截图。R51已在无活动采集/作业时加载本机API/Worker，健康200；包含既有导出清理和R49/R50修复。目标生产服务器仍需更新，不能把本机生效当作目标部署完成。总表只记录当前结论；此前恢复段落见[历史快照](history/2026-09-09-status-before-open-api.md)。用 `git log -1 -- docs/implementation-status.md` 定位总表版本。

## 当前任务与恢复

当前连接及分包回归：新增`test_telnet_runtime_reconnect.py`，真实本地TCP/Telnet在默认十秒无正文后关闭旧客户端、重连并再次初始化；两会话各发送一次定时命令，同run预算恰为2，最终两条服务端连接均收到EOF。新增`test_line_prefix.py`的ANSI CSI/OSC和CRLF跨包用例，精确比对字节、重复正文及每行一个上海时间前缀。主代理连接/存储/归档联合43项及全量957项通过，已提交`d2f5897`。随后新增`test_telnet_heartbeat_failures.py`两项：保活已阻塞于drain时可取消关闭；首个真实Telnet连接注入drain异常后记录错误与CONNECTION_GAP，状态经RECONNECTING进入第二次COLLECTING，停止后两连接EOF。相邻连接16项通过，Ruff全量通过，独立审查无阻塞问题。未修改生产代码、未操作真实设备。该Telnet测试不覆盖登录认证，故障由注入模拟，预算使用MongoMock事务回调，仅证明运行内续计，不替代真实Mongo原子事务、真实网络长期背压或集群容量证据。

最新R52：Coredump查询与下载明确纳入第三方服务账号默认权限。已核实`users/sessions.py`的默认logs:read/logs:download经绑定用户实时继承，现有普通账号无需迁移或重新创建；资源页面入口无管理员/创建者限制。本次明确权限标签、新建服务账号提示和站内API指南，并新增普通Bearer及浏览器票据ASGI鉴权回归，节点下载使用替身，仅证明权限与Range参数转发，不替代真实文件下载摘要证据。相关28项、前端85项及生产构建通过；开发页面mock浏览器1440/390新建账号说明与溢出断言通过，主代理已查看截图。无设备或真实用户操作；既有开发服务保持运行，后端新指南需下次加载源码生效，权限本身已可用。

R49/R50已实现并通过CI：用户密码最小8、Coredump观测状态/扫描/时间语义/flag过滤，证据见[观测修复](history/2026-09-10-coredump-observation.md)。R51已完成代码及35真实重启验证，证据见[离线恢复](history/2026-09-10-offline-recovery.md)；手动STOP/PAUSE与未知运行隔离边界保留。35默认ASH，通过ps准确定位PID后仅一次kill -6生成真实core，挂载、接收、页面查询及下载验证已完成，见[Docker设备实测](history/2026-09-10-docker-nfs-device.md)。两个Docker服务保留并设置unless-stopped；扫描节点accepting=false，不迁移现有任务。真实源core保留，临时验证账户已删除，API下载校验未创建本机副本。

当前阶段：用户已于2026-09-10明确恢复开发，正在实施 NFS/coredump、可读浅层目录和大文件并发下载要求。R32–R41为此前已交付阶段；后文旧验证仅代表原对应提交。

本次实测已收尾：用户原35任务保持COLLECTING，专用重启验证任务STOPPED且无节点归属；flag查询0条，独立Mongo验证库和临时目录已删除。新增`verify_device_coredump.py`仅完成15项脚本回归，实际core证据来自独立API/WS操作，不能把脚本标为实机运行通过。最终审核恢复了收尾异常操作FAILED语义；该最后调整尚未重新加载现有活动Worker，保留正常采集，后续维护窗口加载。第二轮页面因Mac锁屏未重新浏览，正式API已完整核验。

最新确认R47：同一海康设备仅一路正在COLLECTING的SSH任务负责NFS监控；新建其它SSH任务时共享显示“已开启”、禁用重复开关并标明负责任务，不给新任务保存第二份启用配置。复用资源单文档运行租约，新增资源级状态接口与表单轮询；停止、暂停、过期和非本运行状态不得被显示为当前负责人。主代理负责契约/审核/总表，后端与前端子代理分别实现，独立子代理验证真实Mongo互斥。

最新追加R48：修复站内API文档搜索框下方接口分类横向滚动条遮挡分类文字。与R47一起继续完成；只调整该导航滚动区域，保留横向浏览所有分类的能力，并验证桌面/手机滚动后的布局。

协作职责（2026-09-10用户明确）：主代理负责设计、契约、审核、协调调度、集成验收及本总表；子代理负责具体实现。resource_health负责资源认证生命周期和资源前端，api_verify负责NFS部署及挂载会话，task_picker负责coredump目录/固定副本/下载。各模块交付须经过主审与集成测试；不得仅根据子代理报告标记完成。CodeGraph用于修改前索引和影响检查，测试使用项目`.venv`。

资源健康以最新R51为准：OFFLINE导致的系统停止在周期认证恢复ONLINE后自动授权恢复，仍须确认旧连接、节点归属、端点锁及运行记录收尾；AUTH_FAILED/ERROR保持用户更新认证门槛。手动停止和暂停不自动启动，显式resume另有等待新认证路径。34设备既有启动/暂停/恢复/停止证据保留；35两轮真实重启均已恢复采集，第二轮预先接入默认10秒扫描Worker后生成新core，增长观测、稳定状态和下载摘要已验证。

本轮已覆盖目录后段轮转、服务端观测时间、非导出目录副本、取消线程收尾、事务配额及异步导出。本地源目录模拟不能替代真实NFS传输；跨节点高负载和进程故障的完整矩阵仍需验证。

NFS来源规则以最新用户要求为准：固定导出至`*`，不要求设备CIDR，不使用平台IP白名单。旧`NFS_DEVICE_NETWORK`忽略，新环境不生成、Compose不传递。`scripts/configure_nfs_export.py`保留安全绝对目录、服务器IP及实际Worker UID/GID映射；重新部署只覆盖项目专属exports。此前部署定向70项通过；最新监控/NFS部署/验证器31项通过，Ubuntu真实NFS证据见下文。

目录浅层化已在`logs/storage.py`实现为`<root>/<storageIdentity>/<安全任务名>-<完整任务ID>/<YYYY-MM-DD>/<HH>`；`collection/runtime.py`兼容新旧小时路径。主代理独立复跑storage/runtime/maintenance/jobs 63项通过（11.56秒）。旧目录不搬迁；设备身份变更后的实际任务重启目录切换仍须与资源健康联调。

当前边界：此前暂停已由用户手动解除。优先完成本轮新增需求，不把配置字段视为 Worker/NFS、前端或部署闭环已交付。

此前R32–R41的783项后端/70项前端及六套浏览器证据见历史交付记录；不是本轮新功能验收。`872c02b`的远端CI `34441011617`已经核实六作业全部成功，包括核心转储读者及导出真实Mongo验证、完整容器与原生部署；该结果不代表真实NFS或当前孤立回收修改已验收。

导出取消与物理执行状态已分离：`coredumps/jobs.py`在取消后继续刷新执行和作业租约，文件线程及目录收尾完毕才标记FINISHED；维护清理保留有效WRITING作业，失属实例不清理其它执行器目录。审计新增核心转储导出/挂载、资源健康停止的中文摘要、关联资源/IP与一致的失败/取消筛选。下一步复核远端CI并执行目标Linux/设备验收；历史故障原因见本轮集成记录。

快照读者保护已在独立`coredumps/snapshot_readers.py`和`snapshot_lifecycle.py`实现：同catalog行CAS协调读者与清理，RETIRING等待已有读者，DELETING保留路径/令牌直到unlink和配额收尾成功；错误中断后可重试。跨节点导出经受鉴权内部header委托子读者，父退出不影响已授权子读者续传；每块读取检查租约并保留既有限流及完整写入。读者及导出真实Mongo验证已通过容器CI，真实网络长时间故障与主机断电仍未验收。

历史无catalog引用的过期RESERVED/PUBLISHED声明已由`coredumps/snapshot_orphans.py`回收：RECLAIMING保留失败进度，确认无引用后仅删除当前节点fileId/version/token能证明归属的私有core/partial，再幂等归还配额；异常单项记录日志，其它孤儿继续。新版声明保存version直查两个路径，旧声明流式扫描严格匹配；非法标识和符号链接保留不释放。节点/状态/到期及catalog token引用索引避免历史数据全扫描。此改进真实Mongo及远端CI `34442294566`均已通过。

独立Ubuntu NFS验收：`204f9ee`的CI `34443638369`中`nfs-smoke`已成功；主代理收取JSON确认NFSv3/v4直接挂载两个设备IP子目录，双路各67108864字节摘要一致、10001:10001映射、重复配置及重挂成功，清理`rootRemoved/exportRemoved/exportsRefreshed=true`、`preservedPaths/errors=[]`。前一提交`66c3dec`的CI `34443227583`七作业全部成功。本机不安装NFS、不清理真实采集数据；同机双挂载不能替代海康固件、跨服务器或持续大文件容量验收。

历史验收补齐：`e60b85b` 的 Linux CI 34346496395 已核实六作业全部成功，中文截图已收取。该结果不代替本轮新界面测试。

部署边界：2026-09-10已确认期望RUNNING和活动状态任务数均为0后，优雅停止旧API/Worker并加载当前源码。本机8000/8001健康检查均200，5173代理的资源共享监控接口200，local-dev心跳更新正常。前端布局仍以生产页面浏览器证据为准；目标服务器尚需部署。历史PID和任务摘要不能当作当前运行状态。

恢复顺序：对照最新用户消息、本表、git status/log及源码与验证产物。摘要只提供线索，默认值和运行状态必须重新核实。生成交接摘要不是业务任务。保留既有工作，勿重做已完成的命令预算数据库事务；设备socket发送仍有独立不确定性。

最终约束继续有效：设备资源优先、同端口可多任务、SSH默认按账户连接；日志加上海时间前缀、10 MiB顺序分卷、小时压缩包仅含日志，校验发布后删除原卷；资源软删除保留日志。平台IP策略仅限制平台调用方，不限制设备目标。模拟挑战码在忽略的本地密钥文件，不在文档中保存。

全项目尚未完成：500路全天容量与文件可读P99验收、跨节点物理隔离、崩溃遗留作业的持久恢复等继续保留在对应需求行；本轮前端与接口交付不将这些事项标为完成。

## 需求对照

后端简写路径均相对 `backend/camera_logs/`，前端组件位于 `frontend/src/features/`。局部验证通过不等于整体交付。

| ID / 最终需求 | 实现位置与状态 | 验证证据 | 未完成部分 | 下一步 |
| --- | --- | --- | --- | --- |
| R52 第三方默认允许Coredump查询、导出及下载 | 沿用`users/sessions.py`基础logs:read/download和`common/security.py`绑定用户继承；`coredumps/api.py`正式入口；权限标签与站内指南明确包含core | `test_coredump_service_permissions.py`普通scopes空令牌：共享资源查询、本人导出、Bearer全量/Range与浏览器票据准入；他人导出、IP收窄、禁用/删除拒绝；相关28项，前端85项/构建及1440/390弹窗浏览器断言通过 | 节点内容代理替身不证明字节传输，真实传输沿用R43；目标页面文案待部署 | 更新前端及API说明，无需重建已有服务账号 |
| R49 平台密码至少8位 | `users/models.py`及账号/本人改密前端，创建/重置/修改统一8至128；部署文档同步 | 7拒绝/8接受接口与前端测试；本机OpenAPI三个模型minLength8 | 目标服务器待更新 | 更新目标API和前端 |
| R50 Coredump接收状态、时间语义、隐藏flag和子目录发现延迟 | `coredumps/scanner.py`源观测状态和单资源回绕；API过滤历史flag；CoredumpFiles自动刷新与明确时间标签 | 35首轮页面发现/导出/flag过滤通过；第二轮默认10秒扫描预先就绪，重启后新core从2,215,936增长至6,725,382字节，17:12:44稳定；全量和Range源摘要一致，详见Docker实测记录 | 稳定是观测判断非设备完成通知；kill至首见31.8秒含设备生成耗时，非纯扫描延迟；目标服务器需更新，大目录受扫描配额影响 | 目标部署验证；保留源文件，不按flag或catalog新ID推断新core |
| R51 设备离线后卡等待隔离，上线自动恢复原采集任务 | `collection/collector.py`识别AsyncSSH断线；`node/worker.py`同进程同owner收尾重试；`resources/health.py`OFFLINE恢复授权及安全消费 | 后端938项、前端85项/构建；真实Mongo恢复/STOP竞争/锁保护；35真实reboot：OFFLINE约125秒后恢复，新run/session，ONLINE约5秒后COLLECTING，专用任务收尾通过 | 目标服务器需部署；未知旧实例或真实落盘失败不盲目解除BLOCKED；旧无原因marker保守保留 | 目标API/Worker更新并核对遗留任务是否需要外部隔离 |
| R47 同资源单路采集SSH负责NFS，其它新建SSH共享只读开启并显示负责人 | `resources/api.py:coredump-monitor`状态接口；`runtime.py`资源CAS含run/generation/node；`CoredumpMonitorControl.vue`与独立轮询组件，展示不写成新任务配置，站内API已更新 | 主代理后端904项、前端78项/构建；真实Mongo竞争和隔离真实Worker/WS/小时下载通过并清理；整套生产浏览器及共享组件1440/390通过并查看截图；CI34447026933七作业全部通过；本机进程更新后5173代理状态接口200 | 真实设备/跨节点故障矩阵未验收 | 更新目标API/Worker及前端，完成真实设备验证 |
| R48 API文档接口分类横向滚动条不遮挡文字 | `ApiReferenceWorkspace.vue:.api-groups`禁止Flex收缩、稳定滚动槽并保留横向滚动 | 1440旧样式高度16px/按钮底部留白6.28px失败，新样式39px/client留白12px通过；主代理及Linux CI34447026933生产浏览器1440/390/320首尾分类可见、底部留白通过 | 目标服务器需更新前端静态文件，已打开页面需刷新 | 部署更新后保持该布局回归 |
| R42 海康 SSH 可选 coredump、自动 ASH/NFS 挂载及每分钟检查 | `collection/coredump_monitor.py`、`collector.py`、`runtime.py`，原发送队列/会话取消；同资源租约；Docker/原生宿主机NFS固定星号导出 | 35默认ASH，经正式命令队列gdbcfg成功挂载本机Docker NFSv3，mount确认/run/coredump；kill -6生成core时日志继续COLLECTING；Ubuntu v3/v4既有CI有效 | 本次手动命令实测不证明自动一分钟重挂全流程；跨节点隔离和持续大文件未验收；Mac卷/端口代理与Linux宿主部署有差异 | 目标Linux验证自动监控与挂载丢失恢复 |
| R43 coredump多设备大文件接收、查询、批量下载；日志多人稳定下载 | `coredumps/`扫描、冻结、配额、导出；读者委托、清理恢复和孤儿回收；平台/节点Range与原生票据 | 35真实core经不同Worker扫描、平台页面导出；两路完整下载和1MiB Range摘要与NFS源一致；文件名/时间查询正确；既有Ubuntu双64MiB及2GiB×2 HTTP证据保留 | 跨机超大文件、长期混合负载及全部断电窗口未验收；本次真实core约6.72MB；首次发现不是传输结束时间 | 目标Linux持续混合负载验收 |
| R44 海康资源周期认证、设备变更后更新身份及目录，失败停止采集/NFS | `resources/health.py`、资源API及调度：60秒/8并发、探测租约与revision；OFFLINE周期成功自动授权，AUTH_FAILED/ERROR需用户认证；换身份受控新运行 | 独立真实Mongo恢复消费、手动STOP竞争、锁保护通过；35两轮真实离线/上线自动恢复见R51 | 多API进程长期运行、真实换机与凭据更新联调未验收 | 继续目标服务器换机与凭据更新验证 |
| R45 资源编辑回填、类型/IP只读，名称/HTTP认证可编辑 | `ResourceEditor.vue`首次挂载immediate回填，类型/IP只读，空密码保留 | `browser_resource_prefill.mjs`桌面1440及手机390字段/宽度断言通过；主代理已查看390稳定截图，无超宽 | 真实设备编辑认证与任务恢复端到端未验收 | 目标设备联调 |
| R46 第三方暂停设备重启超过一分钟，离线缓存不阻止显式恢复 | `tasks/control.py`、`resources/health.py`：WAITING_DEVICE与新探测；手动暂停意图即使尚未收尾也保留；换机结束旧run但仍可resume；旧探测不能解除新等待；前端状态/操作及站内API指南已接入 | 后端状态机用例包含旧探测后新探测、重复失败不重复操作、暂停收尾中离线；主代理856项全量通过 | 真实重启>60秒的第三方全链路仍待验收 | 目标设备验证实际恢复至COLLECTING才成功 |
| R01 Python/Vue3/Mongo，控制台与第三方共用 API | `main.py`、前端、`pyproject.toml`，已实现 | 源码；历史 CI 34296982222 | 多节点部署未验收 | 部署复核 |
| R02 海康 Digest/Basic 认证，解析型号、短序列号、版本 | `resources/authentication.py`、`resources/api.py`，已实现 | `tests/test_resources.py` | 本轮未重测实体认证 | 401、异常、XML 分支验收 |
| R03 串口资源、多任务、协议/IP 限制，串口可选服务器或自填 | `tasks/resource_binding.py`、资源前端，已实现 | `tests/test_task_resources.py` | 全流程设备验收需补充 | 资源→任务验收 |
| R04 相同设备身份同父目录；软删除保留日志 | `tasks/resource_binding.py`、`resources/lifecycle.py`、`logs/storage.py`，已实现 | 资源/存储测试 | 跨节点仅相对路径一致，非共享物理盘 | 跨节点目录验证 |
| R05 表单、初始化排序、正整数定时参数、IME、密码保留 | `common/models.py`、`TaskEditor.vue`、`CommandEditor.vue`，已实现 | 模型/表单测试，历史浏览器冒烟 | 二次确认见 R16 | 统一交互验收 |
| R06 模板 CRUD/版本/独立副本与计数 | 模板模块、命令编辑器，已实现 | 模板/任务测试 | 目标服务器待部署；软删同创建者名称继续占用 | 等待手动继续后验证目标部署 |
| R07 逐路隔离、有序写入、重复正文保留及行首上海时间 | `collection/collector.py`、`collection/runtime.py`、`logs/storage.py`、`collection/line_prefix.py`，部分验收 | 存储/故障测试及短时摘要报告；ANSI CSI/OSC、CRLF跨包精确字节与重复正文回归，连接/存储/归档联合43项通过 | 500 路全天未证明；SSH PTY 不证明跨独立流时序 | 独立源序号验收 |
| R08 SSH 按凭据连接、不以变化指纹拦截，各协议保活及十秒空闲重连 | `collection/connections.py`、`collector.py`、`runtime.py`；`verify_ssh_lifecycle.py`显式ID、端点分页预检及finally停止 | 本地真实AsyncSSH仅保活时触发默认10秒IDLE_TIMEOUT；真实TCP/Telnet NOP不算正文，旧客户端关闭后重连初始化；drain阻塞取消与注入异常后的错误日志、gap、二次COLLECTING及两连接EOF，相邻16项通过；34实机暂停12秒socket保持0，恢复同run新session/socket1，最终STOPPED且socket0 | 35暂停恢复及真实设备空闲故障、长期背压、集群迁移未验收；Telnet用例未覆盖登录认证 | 继续集群与真实网络故障验收；实机生命周期仅在空闲窗口验证 |
| R09 初始化/定时队列、断线续计、重启归零、发送预算 | `commands/reservation.py`、`collection/runtime.py`，数据库原子性已实现 | 祖先提交`92e01d6`及真实Mongo验证；新增真实Telnet重连测试同run两session各执行一次、预算2，MongoMock回调仅证明续计语义 | socket 不属于数据库事务，设备执行结果仍可未知 | 保持 UNKNOWN、不补发；勿重复实现预算事务 |
| R10 手动优先、不跨会话、断线拒绝与审计 | `commands/manual_submission.py` 将入队、幂等映射和审计同事务提交；已加载本机API | 历史真实副本集证明审计失败/取消整体回滚、同键并发返回同一命令、提交确认丢失后只读恢复、停止后同键重放；临时数据已清理 | 最终 DB 检查至 socket 写入仍需物理隔离；历史 PENDING 仅重放，不补造审计 | 继续 R11 接管隔离 |
| R11 幂等启停、受控重启、租约/代次隔离 | `tasks/editing.py` 编辑、资源声明、停止操作、审计同事务，安全排队编辑保留RUNNING；Worker已知失败保持STOPPED | `175b938` Linux CI 34328314712通过；真实副本集回滚/竞争/确认丢失与排队调度验证；本机API/Worker已更新，34实机暂停恢复停止通过 | 跨节点物理隔离未证明 | 继续旧实例接管隔离，目标Worker部署需受控维护 |
| R12 PSH 密文、ls 探测、模拟口令、失败仅影响当次 | `collection/psh_*.py`、`collector.py`，已部署本机 | 恢复/预算测试及历史实机记录；本轮未发送真实debug | 设备仍处于 Password 时不能发送业务命令；真实解密接口与 10003 切换未验证 | 在具备有效挑战码与接口条件后专门验证，不反复试错 |
| R13 10 MiB 编号分卷、上海小时、仅日志 tar.gz、归档后删原卷 | `logs/storage.py`、`logs/compression.py`，已实现 | 本轮核对校验→发布→同步→unlink；存储测试 | 集群验收未完成；10M 当前按 10 MiB | 保持校验失败保留原卷 |
| R14 小时查询、统一小时包、多选 ZIP、Range | `logs/hour_download.py`、`logs/export_output.py`、日志前端，已实现 | 下载/归档测试、历史浏览器下载 | 分布式缺片与规模限制待验收 | 多小时端到端校验 |
| R15 流式搜索、并发/读预算、配额与到期清理 | `logs/jobs.py`、限制器、`logs/maintenance.py`；导出清理仅允许本节点已完成到期DOWNLOAD，拒绝活跃/取消/未知记录及软链接 | 主代理维护/作业/提交/终态46项通过，包含旧mtime运行目录保护及DB单项失败继续；导出限制测试、合成报告 | 混合持续负载未证明；取消/无记录遗留产物保守保留；当前Worker未加载本次清理修复 | 空闲窗口更新Worker，补持久执行归属和孤立产物恢复 |
| R16 美观 UI、侧栏折叠/滚动、状态按钮、修改二次确认 | 前端 app/features，命令草稿删除已补确认；按对象定位避免异步确认误删；App已拆为499行，列表与导航职责分别位于useWorkspaceCollections/workspaceNavigation/AppNavigation | 当前源码及历史观测修复文档互证，前端无超过500行源文件；最新aa86ce6浏览器CI通过，既有确认/隔离全链路证据保留 | 目标服务器真实设备产品链路未验收；不再把已完成App拆分列为待办 | 保持草稿确认与保存确认独立，仅随新增职责评估拆分 |
| R17 实时虚拟列表/限速、ANSI、暂停跟随与续传 | `LiveLogs.vue`、`LiveLogRanges.vue`、`shared/composables/liveLogBuffer.ts`；已知字节范围补读已实现 | 前端 42 项；浏览器精确五页补读、暂停/任务切换/迟到响应及 1440/390/320 视口；隔离真实 API 的 1200 行/秒模拟流补读与全链路通过 | 服务端无 file/offset 的 gap 事件只能转小时归档；仅保留最近更新的 200 个范围；跨文件范围分别阅读 | 后续完善跨节点/跨文件未知缺口目录定位，继续规模验收；不重复实现已知范围窗口 |
| R18 保留天数、节点登记/准入、审计/事件 | `administration/settings.py` 的保留期/节点配置与审计同事务；`event_presenter.py`、`event_queries.py` 和审计界面提供中文摘要、关联对象、安全原因及 Mongo 派生筛选分页 | 隔离 Cookie 浏览器显示连接缺口、任务/IP、级别/结果与详情；真实 Mongo 派生查询验证与 Linux CI 34324896916 通过；本机三事件接口 200 | 登记不等于部署；缺输入速率准入阈值 | 继续配置生效与速率准入验收 |
| R19 Token/撤销/权限、加密审计、TLS | Token/公共鉴权、`users/sessions.py`；有效用户共享读取，写入按所有者，令牌继承绑定用户，旧独立资源/任务范围模型已替换 | 权限/脱敏/会话及服务令牌测试，本轮全量通过 | 分布式 TLS 未验收 | 真实代理与会话撤销验证，参见R34/R35 |
| R20 500×1200 行/秒×24小时，文件可读 P99≤200ms | 压测工具与写入指标，待验收 | 短时报告仅证明对应样本 | 无等规模证据，API 观察不能替代文件可读 | 独立文件探针与 Linux 集群全天验收 |
| R21 及时清理开发日志，保留证据 | `scripts/cleanup_dev_server_logs.py`、`dev_cleanup_evidence.py`；受限ID维护入口 | 历史清理1,028个副本/601,373,134字节；此前10归档/18 catalog/3,080,799字节预览PROTECTED；本阶段隔离实验自动清理 | 历史两份合成数据保护时点及来源需执行前重新核验，本阶段未物理删除；失败实验仍需完整来源证据 | 暂停；手动恢复后重跑预览，不缩短保护或全局保留期 |
| R22 中文提交、部署/API/运维文档、持续总表 | `AGENTS.md`、hooks、CI、docs，部分交付 | 本轮总表和历史入口，既有提交校验 | 压缩率与全天容量报告未交付 | 每次同步对应状态行 |
| R23 五类 Linux 部署、中文配置、自启动、重复执行保留配置与卷 | 根部署入口、`deploy/*.yml`、`mongo-host-user-init.sh`、`deploy_component.py`；组件后缀隔离项目；systemd 启用 Docker、常驻容器 `unless-stopped` | 本地部署回归 43 项；Linux CI 34324896916 实际完整部署两次及独立四组件部署/重跑/重启；返回 restartHealth、temporaryProjectsRemoved、temporaryDataRemoved 均 true | 未实际重启宿主机；裸机安装 Docker 和 systemd 启用分支为脚本检查；未验收其他发行版与多机部署 | 在目标服务器验收开机启动及真实多机网络；维护已有环境和数据 |
| R24 正常登录、内置管理员、子账户及权限 | `users/`、前端 `features/auth/`、`app/AppNavigation.vue`；含当前用户与本人服务账号入口 | 历史Linux CI；本阶段会话测试、账号浏览器与隔离Cookie普通用户采集全链路通过 | 目标部署后的真实设备验收未完成 | 暂停；后续保持会话代次隔离 |
| R25 平台来源 IP 白名单、多网段独立权限 | `access_policy/`、Nginx、前端 IP 管理，已实现并通过 Linux CI | IPv4/IPv6、权限交集、设备和串口目标不受限测试；CI 真实代理启用/关闭策略及伪造 XFF 检查通过 | 额外反向代理拓扑需单独配置可信来源链 | 部署时按实际代理链检查 clientIp |
| R26 平台访问记录、业务审计与凭据脱敏 | 任务/会话/手动命令/令牌及作业提交取消事务已交付；`logs/job_completion.py`补齐终态审计与重试；本机Worker已更新 | 历史授权CI34344296389和后续基线CI通过；真实副本集四类终态回滚/确认丢失/取消竞争记录；本阶段后端783项通过 | 进程崩溃遗留RUNNING作业的持久恢复未实现 | 完善作业崩溃恢复 |
| R27 Ubuntu/Debian无Docker主机部署、五类入口、详细注释与自启动 | `deploy-native*.sh`、`scripts/native_*.py`、`deploy_native.py`、`docs/native-deployment.md`，已交付；Nginx权限和跨组件合同预检已修正 | `eb4e829` CI34338375626 Ubuntu24.04真实首次/重跑/重启通过，配置不变，临时安装已删除 | 未物理重启宿主机；Ubuntu22.04/Debian12自动依赖分支尚未实机验收 | 目标服务器按文档部署，验收实际依赖源和开机启动 |
| R28 其他电脑通过HTTP服务器IP正常操作 | 共用`frontend/src/shared/api.ts`兼容UUIDv4；Docker/原生前端均重新编译该源码；CI加入`verify_http_browser.mjs` | 先复现相同异常，前端52项/构建通过；真实非安全IPv4 Chrome成功发出启动/命令/下载模拟请求；CI34338375626两类部署通过 | 目标服务器需拉取并重新构建前端；模拟浏览器不等于全部实体设备业务验收 | 更新目标服务器前端并强制刷新，继续产品全链路验收 |

## 验收口径

新增需求对照：

| ID / 最终需求 | 实现位置与状态 | 验证证据 | 未完成部分 | 下一步 |
| --- | --- | --- | --- | --- |
| R29 内网HTTP节点登记保存、节点删除、完整/独立部署配置一致 | `administration/settings.py`、`node_lifecycle.py`、`tasks/claim.py`、设置前端、`deploy_env.py`；已实现并推送 | CI34341798389六作业通过，真实Docker节点登记与持续心跳；真实删除竞争/历史保留/迟到心跳保护；浏览器确认及1440/390截图；本机API/Worker更新后心跳正常 | 目标服务器部署未验收 | 按节点部署排障文档更新原服务，不改已有节点身份或日志目录 |
| R30 明确服务器UTC与上海小时归档的时间关系 | `LogArchives.vue` 标注北京时间UTC+8；原后端归档时区保持上海 | 2026-09-09 18:30:12 UTC归入2026-09-10 02点单测；浏览器同日小时样例显示通过 | 不变更服务器系统时区；不是自动跟随主机时区 | 保持日志前缀、小时目录和显示一致 |
| R31 日志工作台切换不横移、资源行快捷建任务 | `styles.css`稳定滚动槽及移动抽屉父容器宽度、`LogsWorkspace.vue`宽度约束、资源页与App复用任务编辑器；生产页面验证已加入CI | 本机及LinuxCI34346191768生产1440/390布局/提交/权限通过；后续CI34346496395中文截图已收取 | 目标服务器需更新前端 | 部署后复核目标浏览器 |
| R32 资源多条件查询与任务摘要、按时间查询、站内开放API平台 | `resources/api.py`、`logs/time_range.py`、`reference/`、`features/api/`；目录来自真实OpenAPI，支持权限/参数/请求响应示例与搜索 | API/资源发现/时间查询单测；文档1440/390/320浏览器通过 | 目标服务器尚未部署新版本 | 隔离全链路已通过；等待手动继续后验证目标部署 |
| R33 工作台分页选任务、资源筛选、状态/创建人/创建时间 | `LogsWorkspace.vue`、`LogTaskPicker.vue`、`LogResourceFilter.vue` | 工作台1440/390/320浏览器：resourceId筛选、状态轮询、迟到响应、北京时间与所有者按钮 | 隔离模拟链路通过，目标真实设备规模验收未完成 | 等待手动继续后验证目标部署 |
| R34 资源规范IP唯一；资源/任务共享读、创建者及管理员写 | `resources/address_claim.py`、`security.authorize_owner`、任务/资源事务、`shared/ownership.ts` | 单测及真实Mongo12并发仅1成功、占用回滚和软删除释放；审查所有权事务入口 | 本机开发旧库存在重复IP未清理；新增均阻止重复 | 目标空库/受控数据治理；禁止误删历史日志 |
| R35 管理员管理账户；第三方绑定用户、永久有效和用户生命周期 | `users/service_tokens.py`、`users/sessions.py`、下载授权、账户前端；已加载本机API | 永久null合同；停用/删除/权限继承；过期PATCH不能复活；真实事务审计；账号浏览器通过 | 目标服务器尚未部署 | 目标部署验证 |
| R36 个人/指定用户多选共享/管理员全局模板，任务独立快照 | `commands/templates.py`、`users/share-targets`、模板前端、`tasks/creation.py`/`editing.py` | 模板共享与撤销/删除后快照、幂等重放单测；真实owner+name唯一事务；共享浏览器通过 | 目标服务尚未部署 | 部署后复验模板到任务流程 |
| R37 实时日志HTTP/HTTPS双协议与两种部署兼容 | `shared/websocketUrl.ts`供实时页及API示例复用；Docker与原生共用`deploy/nginx/frontend.conf`，TLS示例支持Upgrade | URL单测3项含端口/IPv6；WS路由ws/wss ASGI握手；文档浏览器；代理配置源码核验 | wss真实证书/目标代理尚未实机验收 | 隔离真实ws已通过；目标HTTPS需证书及代理验收 |
| R38 管理员/绑定用户可随时查看服务口令，管理权限不扩大 | `service_tokens.py`加密存储/查看/轮换、`AccessManager.vue`和本人账号入口；已加载本机API | 本阶段后端783项、前端70项；真实Mongo轮换回滚/未知提交/查看审计失败；本人/非本人/改绑/IP/无缓存测试；最终账号生产浏览器及HTTP复制回退通过 | 旧口令只有摘要无法还原，需管理员二次确认后重新生成；目标服务器待部署 | 暂停；等待手动继续后部署 |
| R39 从模板切换其他导航后正文同步切换 | `TemplateList.vue`安全读取共享列表，`commands/templates.py:public_template`补齐读模型；已更新本机API | 缺字段历史夹具复现；修复后生产桌面3轮模板→资源/日志/API/配置及390/320无残留无控制台异常；后端定向17项通过 | 当前已异常的浏览器需刷新一次加载修复 | 目标部署 |
| R40 资源/任务/模板按权限多选批量操作 | `ResourceBatchDelete.vue`、`TaskList.vue`、`TemplateList.vue`与`bulkOperations.ts`；资源/模板批量删除、任务批量启停暂停恢复，串行逐项结果、确认/选择代次及卸载保护 | 单元测试；资源/任务/模板mock覆盖取消零请求、部分失败继续、状态/所有者过滤、页筛选清选与轮询保持；见交付记录 | 批量控制202仅表示已提交，实际执行依赖节点；不跨页批量选择 | 本阶段完成后暂停，等待手动继续 |
| R41 三页创建用户筛选、创建/删除时间、普通默认本人/管理员默认全部 | 三类GET支持createdBy；`users/creators`最小历史用户目录；`CreatorFilter.vue`与三页开关/时间列，统一Asia/Shanghai；模板软删除保留deletedAt | 后端组合筛选、最小字段、历史账号、软删除与快照测试；三页mock默认范围/切换/筛选/时间列 | 已物理删除的旧模板无法恢复；任务显示资源删除时间；软删模板名称仍占用 | 本阶段完成后暂停，等待手动继续 |

| 指标 | 实际测量 | 与原要求关系 |
| --- | --- | --- |
| 原要求文件可读 P99 | 采集端接收到独立读者可读，需明确按行/批权重 | 尚无 500 路全天证据 |
| `common/write_metrics.py` 的 WriteLatency | 首块接收至批量写入完成，批次数加权、滚动秒桶 | 不独立读文件，不含目录/API |
| `scripts/service_benchmark_latency.py` 的 BatchLatency | 源端发送至正式 API 读齐整批，行数加权，含轮询/排队 | 附加端到端指标，不是原文件指标 |
| API 直方图溢出 | 200ms 以上使用观测最大值给保守 P99 上界 | 非精确 P99，超标不能单独证明原指标失败 |

显式 `httpx.Limits(max_connections=...)` 的 `max_keepalive_connections` 为 `None`，不能套用默认构造的 20。依据依赖参数作判断前必须核对实际构造和安装版本。历史 64 路实验未通过 API 延迟与并发窗口，不能宣称容量达标。

## 后续验收

用户已明确恢复需求实现；以下为后续尚未完成的验收，不得与本轮源码交付混为一谈。

1. 目标服务器部署及真实TLS证书/代理、多机节点连通性验收；本机API/Worker已更新，34实机生命周期已验证，35运行任务仅只读验证。
2. R11跨节点物理隔离、R26进程崩溃遗留作业恢复；已完成的数据库事务不重做。
3. R20 Linux集群500路全天容量、独立文件可读P99和压缩率报告。
4. R21历史受保护合成数据在具备来源和到期证据后清理，本阶段隔离实验已自行清理。

旧阶段提交、测试和进程记录见[历史验证记录](history/2026-09-09-status-legacy-validation.md)，不得当作当前进程状态。
