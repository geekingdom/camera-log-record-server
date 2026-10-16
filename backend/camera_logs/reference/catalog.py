"""从实际OpenAPI路径构造完整目录，补齐权限、中文分组及第三方调用说明。"""

from camera_logs.reference.descriptions import describe_operation, describe_parameter, described_schemas
from camera_logs.reference.examples import request_example, response_example, schema_example
from camera_logs.reference.python_examples import build_python_example

GUIDES = [
    {"title": "资源看板", "text": "GET /api/v1/dashboard 需要tasks:read，days只能为7、14或30，date为可选的Asia/Shanghai截止自然日且不能晚于上海今天。响应概览只计算未删除的当前资源和任务；历史日志目录仍可按原归属汇总。所有任务可读用户都会得到overview。只有logs:read时才返回daily、topResources和coredumps；否则这些字段为null且availability.logs=false，不能将其解释为零。只有管理员才得到nodes、节点数和availability.nodes=true；离线节点的accepting固定为false。日志字节来自已登记OPEN/READY分卷的原始bytes，不读取设备正文，也不以共享压缩包archiveBytes重复计数。看板聚合以短期同权限快照合并刷新，超时返回503，调用方应保留上一份有效页面数据后重试。"},
    {"title": "批量添加设备资源", "text": "新建资源的name可省略或留空，服务端使用规范IP作为名称。控制台支持同一地址族的起止IP生成最多256台海康网络设备，共用HTTP认证和监控配置，逐行名称与密码可修改。先调用POST /api/v1/resources/authenticate逐台认证，再通过POST /api/v1/resources/batch提交items列表及Idempotency-Key。保存时服务端重新认证全部设备，全部成功后在同一事务提交资源、IP占用和审计；认证失败或IP冲突不会部分添加。提交结果未知时保留原请求和同一幂等键重试；修改请求后使用新键。密码仅用于输入，不在创建响应中返回。"},
    {"title": "节点性能推荐与手动调整", "text": "GET /api/v1/admin/nodes的每个节点包含performanceRecommendation：available表示当前能否推荐，reason说明依据或不可用原因，policy为可应用的完整性能策略，resources为有效CPU与可用内存，reportedAt为依据心跳时间。只用新鲜节点遥测计算保守建议，容器配额优先于宿主总资源；不测算磁盘最佳吞吐。获取推荐不会修改已有配置；将policy按需编辑后通过PATCH /api/v1/admin/nodes/{node_id}携带version与performance保存，Worker后续心跳热应用。performance为管理员保存值，performanceStatus.effective为实际受资源约束后的值，三者不等同。"},
    {"title": "渐进式历史检索", "text": "POST /api/v1/log-searches冻结文件范围后返回作业。GET /api/v1/log-searches/{identifier}?includeResults=false用于轻量轮询，resultCount为当前已确认结果数；通过/results分页读取。RUNNING中的结果只是已扫描部分，SUCCEEDED且truncated=false才表示本次冻结范围扫描完成。FAILED/CANCELLED/EXPIRED中的已有结果不能证明其余范围没有匹配。普通状态接口默认仍包含结果以兼容现有调用。"},
    {"title": "历史检索全文候选索引", "text": "可选独立Elasticsearch进程从持久文件或归档建立可重建正文副本，不阻塞采集；默认热保留7天，按源小时到期和源目录删除补偿清理。GET /api/v1/tasks/{task_id}/search-index-status展示已覆盖文件与连续水位，未覆盖仍回原文。未启用ES或缺少其完整证据时，沿用小时归档旁SQLite FTS5候选索引（不保存正文、不加入下载包）。索引只用于跳过不可能命中的分卷，最终仍按原始字节确认；索引缺失、损坏、版本或归档指纹不符时自动回退原文扫描。短关键词和高频关键词可能扫描全部原文，结果上限与时间范围限制不变。"},
    {"title": "节点写入延迟限制", "text": "管理员通过POST /api/v1/admin/nodes登记、PATCH /api/v1/admin/nodes/{node_id}带version编辑writeLatencyLimitMs。单位毫秒，整数1至60000，默认200；每节点独立保存，达到75%预警，超过阈值限制新采集会话。保存后调度使用新配置，Worker下一周期更新心跳和准入，无需重启；旧心跳仍拒绝准入时需要等待新心跳。已有采集不因超限被主动停止。提高阈值只调整准入和健康判断，不提高磁盘吞吐、不改变日志顺序、批量写入或同步策略。"},
    {"title": "管理事件游标分页", "text": "audit-events、runtime-events和request-events仅管理员可访问。传cursor=开启游标首屏，使用返回nextCursor续页并保持原筛选和时间范围；返回items、pageSize、hasMore、nextCursor及total。默认不统计总数，total为null，显式includeTotal=true才执行精确统计。未传cursor仍保持page/pageSize旧接口。游标不能跨事件类别或筛选条件使用，页码大于1与游标同时提供返回422。游标是翻页位置而非历史快照，新记录在刷新首屏后可见，保留期内删除的记录不会补回。"},
    {"title": "Coredump时间含义", "text": "receivedAt和firstSeenAt是服务器首次扫描观测时间，不代表文件传输完成。sourceModifiedAt来自设备文件系统mtime，会受设备时间影响，因此可能早于或晚于接收观测时间；不能只比较这两个时间判断丢失或延迟。"},
    {"title": "节点资源准入", "text": "管理员在POST/PATCH /api/v1/admin/nodes配置isGeneralNode，默认true。非通用节点必须填写resourceNetworks多个IP或CIDR，仅允许匹配资源进入，健康且有容量时优先于通用节点；不可用时回退可用通用节点。匹配设备资源IP，不按海康资源下Telnet串口任务的服务器IP匹配。修改后影响新分配，不迁移已有采集任务；Worker无需改造，采样和挂载继续复用采集任务所在节点。"},
    {"title": "设备CPU与内存监控", "text": "创建或编辑海康资源时设置enableResourceMonitor，默认false。有正在采集且sshTarget=HOST的SSH主机，或TELNET_DEVICE任务时每60秒采样；SSH从机和TELNET_SERIAL不参与。ISAPI异常不影响仍有效的采集连接，复用唯一采集连接，不额外建立SSH。GET /api/v1/resources/{identifier}/resource-metrics需要tasks:read，支持带时区start/end半开区间、limit和cursor；默认最近1小时、最大31天，每页最多2000个样本。响应items包含sampledAt、status、values及错误码；CPU单位为%，内存为KB，缺失值不表示零。后台platform-settings.resourceMonitor维护命令与正则，下一轮生效；指标按小时分桶，默认保留90天。"},
    {"title": "设备认证历史", "text": "GET /api/v1/resources/{identifier}/authentication-records需要tasks:read，所有可读用户均可查询海康资源的认证结果。认证历史默认保留90天。同一UTC日内连续、身份未变化且结果相同的周期认证会合并为一条，覆盖SUCCESS、AUTH_FAILED、OFFLINE和ERROR；createdAt为首次认证、latestAt为最近认证、occurrenceCount为次数。结果变化、身份变化、首次认证以及创建、编辑或人工认证均建立新边界。支持result=SUCCESS|AUTH_FAILED|OFFLINE|ERROR、带时区start/end及identityChanged=true|false，多个条件取交集；时间筛选按认证聚合区间与查询范围相交判断，旧单次记录按createdAt回退。响应使用page/pageSize或稳定游标分页。initialAuthentication=true为初始基线，不算设备更换；不会返回密码或原始设备XML。"},
    {"title": "认证失败与任务恢复", "text": "网络设备周期认证得到401时记录AUTH_FAILED并锁存拒绝时间；后续OFFLINE或ERROR不能覆盖该凭据拒绝事实，但authRejectedAt不是永久healthStatus=AUTH_FAILED，周期认证或人工认证得到SUCCESS都会清除该锁存。未被Worker领取的运行任务会按资源健康状态收尾；已领取的会话只在本机确认连接断开或持续10秒无输出时停止，持续输出不会因后台认证失败而被抢停。成功认证只为带有OFFLINE或AUTH_FAILED系统停止标记且仍处于STOPPED的网络任务授权恢复，由调度周期消费；用户停止或暂停的任务没有可消费标记，不会自动重启。Telnet串口不受设备HTTP认证状态和此恢复流程影响。"},
    {"title": "SSH主从初始化与会话保活", "text": "SSH任务的sshTarget默认HOST；SSH主机和TELNET_DEVICE的自动ASH为尽力行为，切换失败不阻塞采集。SLAVE_1至SLAVE_3连接指定内网从机后必须独立确认协议模式和目标身份，主机缓存不会替代从机验证；从机只采集自身输出，退出标记后的主机字节不会混入从机日志。初始化、手动命令、定时命令和保活共用同一会话、接收器与发送队列。SSH主机、已验证的SSH从机和TELNET_DEVICE在正常shell输入空闲60秒后才排入POSIX空操作`:`；从机保活不会新建连接，仍在其已验证的单一shell中发送。发送前再次检查连接、停止状态和PSH命令通道，失败或不可写时跳过并等待。TELNET_SERIAL不发送shell保活；暂停、停止、失败和取消都会取消保活并回收连接。"},
    {"title": "Coredump扫描周期", "text": "Worker默认每5秒扫描目录，上一轮扫描未完成时不重叠执行。显式配置COREDUMP_SCAN_INTERVAL_SECONDS可覆盖默认值；源文件稳定判断仍使用10秒观测窗口，目录扫描频率不等同于传输完成通知。Docker和原生部署使用同一默认值，需更新Worker进程后生效。"},
    {"title": "等待隔离任务恢复", "text": "BLOCKED任务使用POST /api/v1/tasks/{task_id}/restart，正文{\"confirmIsolation\":false}，需要tasks:control且为创建者或管理员。旧Worker先关闭原连接并同步日志，关闭收据核实后释放旧运行，再建立新运行；操作保持PENDING直到COLLECTING。旧节点不可达返回409及ISOLATION_REQUIRED；节点在线但找不到可证明关闭的原实例时，已受理操作的phase为ISOLATION_REQUIRED。仅管理员可在实际隔离该任务旧实例后提交confirmIsolation=true及非空evidence，确认只影响目标任务，不代表整个节点已隔离。不得仅凭心跳或设备重新在线跳过隔离。stop可以取消恢复意图，旧运行关闭或隔离尚未证明时仍保留待收尾状态。资源activeTaskCount只统计COLLECTING且期望RUNNING的任务；unsettledTaskCount用于删除前的待收尾提示，不能当作采集中数量。"},
    {"title": "NFS暂停与停止卸载", "text": "Coredump开关属于海康设备资源。同资源仍有正在采集的SSH主机或Telnet设备任务时，负责人退出只移交控制而不卸载；最后一个适用任务停止或暂停、资源删除或禁用监控时，通过原连接尽力umount -l。ISAPI异常本身不触发卸载。接管沿用原NFS来源，避免跨节点误改挂载路径。仅有效资源租约的原会话可执行清理，离线或卸载超时不阻止任务停止。UNMOUNTED表示已确认挂载表分离，不表示传输文件完整落盘；UNMOUNT_FAILED表示失败或超时，UNMOUNT_SKIPPED表示跳过。操作202仅表示受理，最终PAUSED/STOPPED不等于NFS卸载成功。不会删除已接收core或关闭服务器NFS服务。"},
    {"title": "Coredump默认访问权限", "text": "有效第三方服务账号默认允许Coredump查询、导出和下载，无需额外勾选权限，也无需重新创建已有令牌。账号实时继承绑定用户的logs:read和logs:download基础权限；可查询所有可读设备资源的Coredump文件并为其创建自己的导出，单文件或批量下载均支持。导出作业本身仍仅创建者或管理员可查询、下载、取消；其他用户可针对同一源文件创建自己的导出。平台登录用户的设备资源页同样可进入Coredump查询下载。绑定用户禁用、删除、令牌撤销或到期后不再允许访问，来源IP策略仍会收窄本次有效权限。"},
    {"title": "Coredump副本到期", "text": "固定副本到期后进入RETIRING（等待读取结束），已有下载或导出继续使用其固定版本，新请求可能返回409。全部读者退出后进入DELETING（清理副本），完成后回到RECEIVING。该清理不删除设备写入的NFS源文件；源文件仍存在时可重新创建导出。大文件下载应流式写盘，续传时携带上次ETag作为If-Range，不能把不同固定版本的响应直接拼接。"},
    {"title": "设备重启与暂停恢复", "text": "SSH和Telnet设备暂停释放连接并保留运行预算，Telnet串口不支持暂停或恢复。设备重启期间，周期认证可以显示离线，但不会把用户暂停改成停止。显式resume返回202和操作ID，任务进入WAITING_DEVICE并等待新认证；即使缓存显示ONLINE也会重新探测。等待时可暂停或停止取消；同一设备恢复沿用预算，身份变更后建立新运行。操作到COLLECTING才完成。因OFFLINE或AUTH_FAILED被系统停止且保留恢复标记的网络任务，可由后续成功的周期认证或人工认证授权恢复；手动停止和暂停任务不自动启动。"},
    {"title": "Coredump接收与下载", "text": "海康网络资源设置enableCoredumpMonitor=true后由活跃SSH或TELNET_DEVICE任务监控；节点配置NFS_ROOT和NFS_SERVER_IP，设备来源不受平台IP白名单限制。GET /api/v1/resources/{resource_id}/coredump-monitor在资源开关、租约、采集状态和节点心跳有效时返回owner。GET /api/v1/resources/{resource_id}/coredumps按name、receivedFrom/receivedTo查询，隐藏coredump_flag.cdf。receivedAt/firstSeenAt是服务器首次观测，不代表传输完成；sourceModifiedAt为设备文件mtime。sourceState/sourceObservedAt/sourceUnchangedSince/sourceStableAt表示源元数据稳定观测；RECEIVING为已观测源文件，FROZEN为服务器固定副本，不证明设备完成崩溃文件。POST /api/v1/coredump-exports提交fileIds及Idempotency-Key，轮询作业；单文件原样、多文件ZIP STORE。GET /api/v1/coredump-exports/{identifier}/content支持Range及If-Range，下载应流式落盘。"},
    {"title": "查看服务账号口令", "text": "管理员可查看全部服务账号，普通用户只可查看绑定给本人的账号。GET /api/v1/service-tokens和POST /api/v1/service-tokens/{id}/reveal要求service-tokens:read；列表不含口令，reveal返回token并记录无敏感内容的审计。新口令加密保存可重复查看，旧版仅保存摘要的口令无法还原。管理员可用POST /api/v1/service-tokens/{id}/rotate携带version重新生成，旧口令立即失效；普通用户不能新增、编辑、撤销或重新生成。永久有效不绕过用户禁用、删除和来源IP限制。"},
    {"title": "第三方鉴权", "text": "管理员通过服务令牌接口创建可撤销Token。普通HTTP请求携带Authorization: Bearer <SERVICE_TOKEN>。Token范围与平台来源IP权限取交集；IP白名单只约束调用方，不约束设备IP。接口目录不授予接口执行权限。"},
    {"title": "资源发现与任务关联", "text": "GET /api/v1/resources 的search匹配资源名、IP、型号、序列号和软件版本。name、model、subSerialNumber、softwareVersion是字面子串；ip精确匹配；同时传入的条件取交集。每项包含tasks摘要，默认100条、taskLimit最大500；tasksTruncated=true时通过tasksUrl继续分页。拥有tasks:read的有效用户可读取全部资源和任务，不再按资源或任务ID白名单收窄可见范围。"},
    {"title": "按任务ID控制", "text": "POST /api/v1/tasks/{task_id}/start、stop、pause、resume返回202及operation id；轮询GET /api/v1/operations/{identifier}确认完成。pause/resume支持SSH和Telnet设备，Telnet串口不支持；暂停释放连接并保留运行预算，恢复重新初始化；停止后启动建立新运行。202不代表设备已经连接。"},
    {"title": "定时命令逐项记录", "text": "GET /api/v1/tasks/{task_id}/command-executions按page/pageSize分页，commandId按单项定时配置筛选，kind可为MANUAL或SCHEDULED。记录保存command正文、totalExecutions、intervalSeconds及attempt快照；scheduledCommands返回当前runId内每项独立attempts预算，同正文不同ID不合并。commandSource=SNAPSHOT表示历史快照，CURRENT_CONFIGURATION表示依相同配置ID恢复旧记录正文，UNAVAILABLE表示无法还原。任务编辑会重建定时配置ID，不以新正文覆盖旧执行；发送次数不代表设备业务成功次数。"},
    {"title": "NFS支持协议", "text": "海康网络资源设置enableCoredumpMonitor=true后，由正在采集的SSH或TELNET_DEVICE任务共用资源唯一负责人租约；任务不再接受独立开关，TELNET_SERIAL不参与。复用采集连接和发送队列完成ASH确认、gdbcfg挂载、每分钟检查与最后任务退出后的卸载；挂载失败不阻断日志采集。"},
    {"title": "幂等、版本与错误", "text": "创建资源、任务、模板、下载、检索和发送命令必须传Idempotency-Key，长度1至128。同键同请求重放，不同请求冲突409。编辑及节点删除需当前version。错误统一包含error.code/message/requestId；422还包含details。401为认证失效，403为权限不足，503为暂不可确认。"},
    {"title": "按时间查询日志", "text": "POST /api/v1/log-searches提交taskId、start、end，可省略keyword。时间为带时区ISO8601，最多24小时，省略时默认最近一小时。空keyword按采集块接收时间的[start,end)返回字节片段，每片不超过4096字节，data为Base64，按fileId与offset无损拼接；text仅供预览。无完整时间索引则失败，不能猜测日志时间。非空keyword为字面检索。"},
    {"title": "查询结果与完整日志", "text": "检索返回作业id；查询作业状态后，通过/log-searches/{id}/results分页获取结果。非空关键词按完整匹配行返回text，同一行多个命中只返回一项，不包含相邻行和行尾换行符；fileId/offset为首个范围内命中的原始字节位置，lineStartFileId/lineStartOffset为逻辑行起点，可跨连续分卷。单行最多256KiB，命中超长行明确失败；结果最多1000项且正文总计最多8MiB，truncated=true表示需缩短区间或读取原文件。GET /api/v1/log-files/{id}读取安全元数据，再按文件/content?offset=0&limit=65536续读，limit最大262144。空关键词仍返回按接收时间筛选的Base64字节片段。搜索上限和页面着色不影响原始日志保存。"},
    {"title": "小时下载与断点续传", "text": "先用/tasks/{task_id}/log-days?month=YYYY-MM取得上海自然日目录，再按选中日期调用/tasks/{task_id}/log-hours取得hourId并POST /downloads提交taskId和hourIds。月目录只返回有OPEN或READY且登记字节数大于零的日期，不读取日志正文。单次最多168小时、预计20GB。轮询成功后GET /downloads/{id}/content，携带Bearer及可选Range。单小时下载仅含日志的tar.gz，多小时ZIP STORE。缺片默认失败；allowPartial=true才允许部分导出。"},
    {"title": "实时日志", "text": "HTTP部署使用ws://<host>/api/v1/tasks/{task_id}/logs；HTTPS部署使用wss://<host>/api/v1/tasks/{task_id}/logs。连接后10秒内发送JSON首帧token及可选cursor，不能把Token放URL。服务端每次推送重验权限；data为Base64原始字节，fileId/offset用于消除重叠后再解码。gap表示实时缓冲缺口，按文件或小时归档补读。重连提交最后cursor，不保证断线期间设备输出可追回。"},
    {"title": "实时日志显示配置", "text": "GET /api/v1/display-settings 供已登录平台用户和有效第三方服务账号读取实时日志浏览器内存上限 liveLogBufferMiB。该接口只返回整数1至100的MiB上限，默认10；不返回日志保留期、节点容量、资源监控规则或其他管理员后台配置。前端应定期刷新该值并只限制本地展示缓存，不能据此删除Worker原始日志、改变采集顺序或中断下载。"},
    {"title": "手动命令", "text": "POST /tasks/{task_id}/commands提交command及可选newline/delaySeconds/prompt/timeoutSeconds，使用同一采集连接。初始化未完成或断线拒绝；通过/commands/{id}或/task的command-executions查询。最多发送次数不代表设备业务成功，UNKNOWN不自动重发。"},
    {"title": "用户权限与所有权", "text": "有效普通用户自动拥有tasks:read、logs:read、logs:download、templates:read、templates:write和service-tokens:read；用户配置的scopes仅增加写入、控制、命令或管理能力。平台来源IP策略仍会收窄本次请求的有效权限。服务令牌实时继承绑定用户当前权限、管理员身份和启用状态。所有人可读取资源、任务、下载和只读日志；资源和任务的编辑、删除或控制，以及向任务发送手动命令，均须具备对应scope且仅创建者或管理员可执行。创建任务可使用他人创建的资源，任务自身独立归属。"},
    {"title": "模板共享", "text": "服务端写入模板创建者，客户端不能提交createdBy。创建者、sharedWith中的有效用户及sharedWithAll=true时的全部有效用户可读取模板；管理员可读取全部模板。仅创建者或管理员可编辑、删除模板；sharedWith中的用户必须存在、启用且未删除，sharedWithAll仅管理员可设置。模板名称按创建者唯一。任务只在创建或实际切换模板来源时校验模板可读，已保存的任务快照不会因撤销共享或删除模板失效。GET /api/v1/users/share-targets要求templates:read，返回可共享对象的最小用户列表。"},
    {"title": "示例与运行环境", "text": "下列内容均为虚拟示例，标识需替换为实际返回值。成功响应展示代表字段，可能包含更多运行信息。文档与代码一同部署，不请求GitHub或外部CDN。这里不自动执行接口；实际调用会按权限记录审计。"},
]

TITLES = {"get": "查询", "post": "创建", "patch": "编辑", "delete": "删除"}
SPECIAL = {"start": "启动任务", "stop": "停止任务", "pause": "暂停网络设备任务", "resume": "恢复网络设备任务",
           "authentication-records": "查询设备认证记录", "resource-metrics": "查询设备 CPU 与内存指标",
           "restart": "重新启动等待隔离任务", "log-gap-catalog": "定位实时缺口已保存片段",
           "creators": "查询历史创建用户",
           "reveal": "查看服务账号口令", "rotate": "重新生成服务账号口令",
           "credential": "查看设备资源登录凭据",
           "authenticate": "认证设备资源", "login": "账号登录", "logout": "退出登录", "password": "修改本人密码",
           "me": "查询当前登录用户", "reset-password": "重置子账户密码", "confirm-isolation": "确认旧节点已隔离",
           "browser-session": "签发浏览器下载授权", "results": "分页查询检索结果", "log-hours": "查询任务小时日志", "log-days": "查询任务日志日期",
           "command-executions": "查询任务命令记录", "permissions": "查询可配置权限", "share-targets": "查询模板共享对象", "display-settings": "查询实时日志显示配置", "content": "读取日志内容"}

_OPERATION_TITLES = {
    ('GET', '/health'): '检查API服务健康状态',
    ('GET', '/metrics'): '读取Prometheus监控指标',
    ('GET', '/api/v1/api-reference'): '获取API接口文档目录',
    ('GET', '/api/v1/users'): '查询用户列表',
    ('POST', '/api/v1/users'): '创建用户账号',
    ('PATCH', '/api/v1/users/{identifier}'): '修改用户账号',
    ('DELETE', '/api/v1/users/{identifier}'): '删除用户账号',
    ('GET', '/api/v1/tasks'): '分页查询采集任务列表',
    ('GET', '/api/v1/tasks/{task_id}'): '查询单个采集任务详情',
    ('GET', '/api/v1/operations/{identifier}'): '查询任务控制操作执行状态',
    ('GET', '/api/v1/resources'): '分页查询设备资源列表',
    ('GET', '/api/v1/resources/{identifier}'): '查询单个设备资源详情',
    ('GET', '/api/v1/resources/{identifier}/coredump-monitor'): '查询设备Coredump监控与挂载状态',
    ('POST', '/api/v1/resources/authenticate'): '认证待添加设备凭据',
    ('POST', '/api/v1/resources/{identifier}/authenticate'): '立即重新认证已有设备资源',
    ('GET', '/api/v1/command-templates'): '分页查询命令模板列表',
    ('GET', '/api/v1/command-templates/{identifier}'): '查询单个命令模板详情',
    ('POST', '/api/v1/tasks/{task_id}/commands'): '向采集会话发送手动命令',
    ('GET', '/api/v1/commands/{identifier}'): '查询手动命令执行状态',
    ('GET', '/api/v1/nodes'): '查询可见采集节点列表',
    ('GET', '/api/v1/service-tokens'): '查询服务账号令牌列表',
    ('DELETE', '/api/v1/service-tokens/{identifier}'): '撤销服务账号令牌',
    ('GET', '/api/v1/log-files/{identifier}'): '查询日志分卷元数据与归档状态',
    ('GET', '/api/v1/log-files/{identifier}/content'): '读取日志分卷原文字节',
    ('GET', '/api/v1/downloads/{identifier}/content'): '下载已生成的日志归档文件',
    ('GET', '/api/v1/resources/{resource_id}/coredumps'): '查询设备Coredump文件列表',
    ('POST', '/api/v1/coredump-exports'): '创建Coredump批量导出作业',
    ('GET', '/api/v1/coredump-exports/{identifier}'): '查询Coredump导出作业状态',
    ('DELETE', '/api/v1/coredump-exports/{identifier}'): '取消Coredump导出作业',
    ('POST', '/api/v1/coredump-exports/{identifier}/browser-session'): '签发Coredump导出文件浏览器下载授权',
    ('POST', '/api/v1/coredumps/{identifier}/browser-session'): '签发单个Coredump浏览器下载授权',
    ('GET', '/api/v1/coredump-exports/{identifier}/content'): '下载已生成的Coredump导出文件',
    ('GET', '/api/v1/coredumps/{identifier}/content'): '下载单个Coredump文件',
    ('GET', '/api/v1/audit-events'): '查询业务操作审计记录',
    ('GET', '/api/v1/runtime-events'): '查询采集运行事件',
    ('GET', '/api/v1/request-events'): '查询HTTP请求访问记录',
    ('GET', '/api/v1/admin/nodes'): '查询节点管理列表及性能配置',
    ('POST', '/api/v1/admin/nodes'): '登记采集节点',
    ('PATCH', '/api/v1/admin/nodes/{node_id}'): '修改节点准入与性能配置',
    ('DELETE', '/api/v1/admin/nodes/{node_id}'): '注销采集节点',
    ('POST', '/api/v1/downloads/{identifier}/browser-session'): '签发日志归档浏览器下载授权',
    ("POST", "/api/v1/log-searches"): "创建历史检索作业",
    ("GET", "/api/v1/log-searches/{identifier}"): "查询历史检索作业状态",
    ("GET", "/api/v1/log-searches/{identifier}/results"): "分页读取历史检索结果",
    ("DELETE", "/api/v1/log-searches/{identifier}"): "取消历史检索作业",
    ("POST", "/api/v1/downloads"): "创建日志下载作业",
    ("GET", "/api/v1/downloads/{identifier}"): "查询日志下载作业状态",
    ("GET", "/api/v1/tasks/{task_id}/downloads"): "分页读取任务下载历史",
    ("DELETE", "/api/v1/downloads/{identifier}"): "取消日志下载作业",
    ("GET", "/api/v1/tasks/{task_id}/search-index-status"): "查询历史检索索引覆盖状态",
}


def group(path):
    """按正式路径归类，不依赖前端功能是否对当前用户展示。"""
    for fragment, title in (("dashboard", "资源看板"), ("coredump", "Coredump文件"), ("resources", "设备资源"), ("command-templates", "命令模板"),
                             ("commands", "命令交互"), ("command-executions", "命令交互"),
                             ("log-", "日志查询"), ("search-index", "日志查询"), ("downloads", "日志下载"), ("tasks", "采集任务"),
                             ("operations", "异步操作"), ("service-tokens", "服务令牌"),
                             ("users", "用户管理"), ("auth/", "登录会话"), ("events", "审计事件"),
                             ("nodes", "节点管理"), ("ip-policy", "来源IP策略"),
                             ("platform-settings", "平台配置"), ("display-settings", "平台配置")):
        if fragment in path:
            return title
    return "平台接口"


def permission(path, method):
    """标明实际scope、所有权和会话例外，避免目录沿用已移除的ID白名单。"""
    if path == "/health":
        return "无需Token"
    if path == "/api/v1/dashboard":
        return "tasks:read；logs:read决定日志统计可用性，admin决定节点信息可见性"
    if "/auth/" in path:
        if path.endswith("/login"):
            return "无需预先认证（仍受平台来源IP策略约束）"
        if path.endswith("/logout"):
            return "无需预先认证；携带登录会话Cookie时撤销该会话"
        return "仅Cookie登录会话"
    if path == "/api/v1/users/share-targets":
        return "templates:read"
    if path == "/api/v1/users/creators":
        return "tasks:read 或 templates:read（最小历史创建人目录）"
    if path.endswith("/search-index-status"):
        return "logs:read"
    if path == "/api/v1/display-settings":
        return "有效Token或登录会话（仅返回实时日志内存上限）"
    if "/service-tokens" in path and (method == "GET" or path.endswith("/reveal")):
        return "service-tokens:read + 仅绑定用户本人或admin"
    if any(part in path for part in ("/users", "/service-tokens", "/admin/", "/nodes", "-events", "/platform-settings")) or path == "/metrics":
        return "admin"
    if "api-reference" in path:
        return "有效Token或登录会话"
    if "coredump" in path:
        if path.endswith("/coredumps"):
            return "logs:read"
        return "logs:download" + (" + 导出创建者或admin" if "/coredump-exports/" in path else "")
    if "/resources" in path:
        if method == "GET":
            return "tasks:read"
        if path.endswith("/credential"):
            return "resources:write + 仅创建者或admin（明文凭据，不缓存）"
        if path == "/api/v1/resources/{identifier}/authenticate" and method == "POST":
            return "resources:write + 仅创建者或admin；空请求体另需tasks:control且非管理员不得影响他人关联任务，完整ResourceInput仅预览"
        if method == "POST":
            return "resources:create"
        return "resources:write + 仅创建者或admin" + (" + tasks:control" if method == "DELETE" else "")
    if "/command-templates" in path:
        if method == "GET":
            return "templates:read"
        return "templates:write" if method == "POST" else "templates:write + 仅创建者或admin"
    if "/downloads" in path:
        return "logs:read（取消本人作业；他人作业另需admin）" if method == "DELETE" else "logs:download"
    if "/log-searches" in path and method == "DELETE":
        return "logs:read（取消本人作业；他人作业另需admin）"
    if "/log-" in path or path.endswith("/logs"):
        return "logs:read"
    if path.endswith("/commands") and method == "POST":
        return "commands:send + 仅任务创建者或admin"
    if path.endswith("/restart"):
        return "tasks:control + 仅任务创建者或admin；confirmIsolation=true仅admin"
    if any(path.endswith("/" + action) for action in ("start", "stop", "pause", "resume")):
        return "tasks:control + 仅任务创建者或admin"
    if path.endswith("/tasks") and method == "POST":
        return "tasks:create；autoStart=true另需tasks:control"
    return "tasks:write + 仅任务创建者或admin" if method == "PATCH" else "tasks:read"


def request_headers(path, method, schema, idempotent):
    """按实际鉴权入口生成调用头，避免把登录和浏览器下载误写成 Bearer 专用。"""
    if path == "/health":
        headers = {}
    elif path.endswith("/auth/login"):
        headers = {"X-Requested-With": "XMLHttpRequest"}
    elif path.endswith("/auth/logout"):
        headers = {"X-Requested-With": "XMLHttpRequest", "Cookie": "camera_session=<登录会话Cookie>"}
    elif path.endswith("/auth/me"):
        headers = {"Cookie": "camera_session=<登录会话Cookie>"}
    elif path.endswith("/auth/password"):
        headers = {"X-Requested-With": "XMLHttpRequest", "Cookie": "camera_session=<登录会话Cookie>"}
    elif path.endswith("/content") and any(fragment in path for fragment in ("/downloads/", "/coredumps/", "/coredump-exports/")):
        headers = {"Authorization": "Bearer <SERVICE_TOKEN>（或 camera_session / download_access Cookie）"}
    elif "api-reference" in path:
        headers = {"Authorization": "Bearer <SERVICE_TOKEN>（或已登录会话 Cookie）"}
    else:
        headers = {"Authorization": "Bearer <SERVICE_TOKEN>"}
    if idempotent:
        headers["Idempotency-Key"] = "example-unique-request-key"
    if schema:
        headers["Content-Type"] = "application/json"
    return headers


def operation_title(method, path):
    """为相同资源的创建、状态、结果和取消操作保留可区分的动作名称。"""
    return _OPERATION_TITLES.get(
        (method, path),
        "资源运行看板" if path == "/api/v1/dashboard"
        else "批量创建设备资源" if path == "/api/v1/resources/batch"
        else SPECIAL.get(path.split("/")[-1], TITLES.get(method.lower(), method) + group(path)),
    )


def catalog(app):
    """完整枚举公共HTTP接口，再补充OpenAPI没有表达的WebSocket握手与消息。"""
    spec = app.openapi()
    # 文档目录使用独立副本补齐中文语义，不能污染 FastAPI 用于接口校验的 OpenAPI 缓存。
    schemas = described_schemas(spec.get("components", {}).get("schemas", {}))
    operations = []
    for path, methods in spec["paths"].items():
        for method, definition in methods.items():
            if method not in {"get", "post", "patch", "delete", "put", "head", "options"}:
                continue
            verb = method.upper()
            schema = definition.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            code = min(int(code) for code in definition["responses"] if code.startswith("2"))
            title = operation_title(verb, path)
            idempotent = method == "post" and (path == "/api/v1/resources/batch" or path.split("/")[-1] in {"resources", "tasks", "command-templates", "downloads", "log-searches", "commands", "coredump-exports"})
            headers = request_headers(path, verb, schema, idempotent)
            parameters = [{**item, "description": describe_parameter(item),
                           "example": schema_example(item.get("schema", {}), schemas, item["name"])}
                          for item in definition.get("parameters", [])]
            operations.append({"id": f"{verb} {path}", "method": verb, "path": path, "title": title,
                               "group": group(path), "description": describe_operation(
                                   verb, path, definition.get("description", "按当前权限执行操作，响应字段随状态变化。"),
                               ),
                               "permission": permission(path, verb), "headers": headers, "parameters": parameters,
                               "requestSchema": schema, "requestExample": request_example(schema, schemas) if schema else None,
                               "responseStatus": code, "responseExample": response_example(path, verb, code)})
    operations.append({"id": "WS /api/v1/tasks/{task_id}/logs", "method": "WS", "path": "/api/v1/tasks/{task_id}/logs",
                       "title": "订阅任务实时日志", "group": "日志查询", "description": next(item["text"] for item in GUIDES if item["title"] == "实时日志"),
                       "permission": "logs:read", "headers": {}, "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "string", "description": describe_parameter({"name": "task_id"})}, "description": describe_parameter({"name": "task_id"}), "example": "task-example"}],
                       "requestSchema": None, "requestExample": {"token": "<SERVICE_TOKEN>", "cursor": ""},
                       "responseStatus": 101, "responseExample": {"type": "data", "fileId": "file-example", "offset": 0, "data": "bG9nCg==", "cursor": "<续传游标>"}})
    for operation in operations:
        operation["pythonExample"] = build_python_example(operation)
    return {"version": "1.1", "guides": GUIDES, "operations": operations, "schemas": schemas,
            "errorExample": {"error": {"code": "403", "message": "权限不足", "requestId": "request-example"}}}
