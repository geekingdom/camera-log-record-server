# API 使用说明

管理员节点隔离确认接口 `POST /api/v1/nodes/{id}/confirm-isolation` 使用副本集事务提交节点、任务及审计变更。仍有新鲜心跳返回 409；数据库事务失败返回 503，客户端应先查询节点状态再重试。操作者必须先确认外部基础设施隔离，接口不会执行物理隔离。恢复运行的任务保留原运行和命令预算，暂停任务保持暂停；配置编辑引起的重启使用新运行。未知发送结果不会自动重发。

公开业务接口以 `/api/v1` 开头。控制台使用登录后签发的 HttpOnly Cookie，第三方使用 Bearer Token：

```http
Authorization: Bearer <access-token>
```

创建设备资源、任务、模板、手动命令、搜索和下载作业要求 `Idempotency-Key`；编辑使用版本号，启停接口按期望状态幂等处理。以下值均为示例，不能作为真实凭据使用。

## 登录与会话

`POST /api/v1/auth/login` 接收 `username`、`password`；`GET /api/v1/auth/me` 查询当前登录身份；`POST /api/v1/auth/logout` 退出；`POST /api/v1/auth/password` 接收 `currentPassword`、`newPassword`。浏览器写请求需 `X-Requested-With: XMLHttpRequest` 且来源匹配。登录路由不要求既有身份，但受客户端 IP 策略和登录频率限制。内置管理员首次登录需改密。

登录事务重新检查验密时的认证版本、密码摘要及账号启用状态，将旧会话撤销、新会话和登录审计一起提交；账号并发变更返回 409。自身改密同时提交新密码、认证版本、新会话及审计，数据库写入失败不会单独改变密码。退出将会话撤销与审计原子提交，无匹配会话时幂等返回 204。Cookie 仅在提交已确认后设置或删除，令牌明文不进入数据库或审计。

提交确认异常时仅通过本次固定审计标识及会话状态作多数主库只读恢复，不重新发起写入；不能确认返回 503 且不发送新的 Cookie。客户端可重新登录确认密码，或重试退出。数据库提交后发生网络断开或 Cookie 响应失败不代表事务回滚。账号停用、删除、权限/密码版本变更在后续鉴权时即时生效，多浏览器会话仍被允许。

## 设备资源

新任务必须先有设备资源。`POST /api/v1/resources/authenticate` 预览认证，`POST /api/v1/resources` 保存时重新执行服务端认证；后者需要幂等键。海康请求示例：

```json
{"name":"机房摄像机","kind":"HIKVISION_NETWORK","ip":"192.0.2.10","username":"admin","password":"<http-password>","authType":"DIGEST"}
```

`authType` 默认 `DIGEST`，也支持 `BASIC`。成功返回 `model`、`subSerialNumber`、`softwareVersion`，创建结果另含资源 `id`。设备最终返回 401 时接口报告设备凭据错误；超时、其他非 200 和非法 XML 报告设备异常，均不会创建资源。串口服务器创建请求只需：

```json
{"name":"机房串口服务器","kind":"SERIAL_SERVER","ip":"192.0.2.8"}
```

`GET /api/v1/resources?page=1&pageSize=20&kind=HIKVISION_NETWORK&search=机房` 分页查询，`GET /api/v1/resources/{id}` 查询详情。读写沿用 `tasks:read`、`tasks:write` 权限，受任务白名单限制的账号不能创建资源或执行认证探测。完整连接与目录约束见 [设备资源说明](device-resources.md)。

`PATCH /api/v1/resources/{id}` 提交上述完整资源字段及 `version` 修改名称或 HTTP 凭据；空密码表示保留原值，IP、类型和物理设备身份不允许改变。`DELETE /api/v1/resources/{id}?version=1` 返回 202 并软删除资源，需要 `tasks:write` 和 `tasks:control`；受任务白名单限制的令牌不能编辑或删除共享资源。关联任务受控停止，任务记录和已有日志不删除。重复 DELETE 可重试未完成的停止请求，调度器也会补偿。

列表及详情包含 `taskCount`、`activeTaskCount`，受限令牌只看到其授权任务计数。列表默认排除已删除资源，增加 `includeDeleted=true` 可查询；已删除资源详情保留 `deletedAt`，`deletionState=PENDING` 表示关联任务仍在停止或等待回收，所有任务停止且运行锁释放后才标记 `DONE`。文件查询和下载接口保持可用。

## 任务

创建采集任务：

```http
POST /api/v1/tasks
Idempotency-Key: 46a0f16d-8c5c-4b05-a9d4-2d8b972030bf
Content-Type: application/json

{
  "name": "机房摄像机 01",
  "resourceId": "<saved-resource-id>",
  "protocol": "SSH",
  "ip": "192.0.2.10",
  "port": 22,
  "username": "operator",
  "password": "<device-password>",
  "initialCommands": [{"command": "show log", "newline": "\n"}],
  "scheduledCommands": []
}
```

响应包含服务端生成的 `id`、`version`、`status` 和 `desiredState`，不会返回 `password` 或 `passwordEncrypted`。列表接口为 `GET /api/v1/tasks?page=1&pageSize=20`；单项读取与更新分别为 `GET`、`PATCH /api/v1/tasks/{taskId}`。更新 body 必须带当前 `version`，版本过期返回 `409`。

任务编辑把配置、关联资源声明、必要的停止操作和审计放入同一事务。暂停意图已接受时，即使实际状态尚未变成暂停，也只允许修改名称和说明。已分配节点的运行任务修改连接或命令时，需要 `tasks:control`，返回任务包含 `controlOperationId`，可查询对应停止操作；旧运行正常结束后才按新配置重启。旧运行有故障则操作失败、任务保持 `ERROR/STOPPED`，需排查后显式启动。无节点的排队任务在确认不存在遗留运行锁及未结束运行后，直接更新配置并保留启动意图。事务确认未知返回 503，客户端先重新查询版本与配置，不盲目使用旧版本重试。

列表可使用 `resourceId` 筛选所属资源。所有任务必须带 `resourceId`，缺失返回 422；保存后不能更换所属资源。海康 SSH/Telnet 设备任务和串口服务器串口任务的 IP 必须匹配资源。海康的 Telnet 串口任务支持 `serialServerResourceId` 选择已添加串口服务器，IP 必须匹配；省略或置 null 则允许自定义串口目标 IP。端口无重复限制，仍须是 1–65535 的整数。本版使用统一资源结构，不提供历史独立任务的兼容或迁移接口。

SSH 任务默认可直接启动：服务以任务的 `username` 和 `password` 认证，不要求请求携带主机指纹、`known_hosts` 内容或任何登记确认。默认部署不校验 SSH 主机密钥，因此同一 IP 和端口的设备替换或密钥轮换不会使任务因指纹变化被拒绝。主机密钥严格校验仅能由部署设置 `SSH_VERIFY_HOST_KEY=true` 启用；该部署模式下无效 `KNOWN_HOSTS` 文件或已登记密钥失配会使 SSH 会话失败，客户端仍无需通过 API 管理指纹。

启动和停止：

```http
POST /api/v1/tasks/{taskId}/start
POST /api/v1/tasks/{taskId}/stop
```

两者返回操作对象；可通过 `GET /api/v1/operations/{operationId}` 查询状态。

SSH 任务还可暂停和恢复：

```http
POST /api/v1/tasks/{taskId}/pause
POST /api/v1/tasks/{taskId}/resume
```

暂停仅适用于正在运行的 SSH 任务。Telnet 设备和串口任务调用 pause 会返回 `409`。

### 控制操作与事务边界

控制请求要求 `tasks:control`，返回的操作对象包含 `id`、`taskId`、`desiredState`、`action`（start/stop/pause/resume）、`actor`、`status`、`createdAt`，实际达到目标时另有 `completedAt`。审计动作分别为 `control:RUNNING`、`control:STOPPED` 和 `control:PAUSED`，审计动作不包含设备地址、口令或命令正文。

创建任务（包括 `autoStart=true`）将任务、幂等成功映射、可选自动启动操作和审计一起原子提交。提交确认未知时使用同一 `Idempotency-Key` 重试，返回原任务；自动启动响应的 `operationId` 始终关联最初创建的启动操作，不会因后续停止/继续而改变。

服务会在同一 MongoDB 快照事务中写入任务控制声明、关联资源声明、相反 PENDING 操作取消、新操作、任务 `desiredState` 与成功审计。重复请求同一已接受状态会复用任务 `controlOperationId` 指向的 PENDING 操作；只有目标已经实际达到时才复用 SUCCEEDED 操作。重复 `resume` 也是此规则：它只复用已接受的 RUNNING 恢复操作，不会新建运行或重置原运行的命令预算。

控制意图并不等于设备连接已经建立、暂停或关闭。Worker 仍负责实际连接和物理收尾；客户端应查询 `GET /api/v1/operations/{operationId}` 及任务状态。事务提交结果无法确认时接口返回 `503`，客户端应先查询任务和操作，不能把该响应当作可以无条件重发控制请求的承诺。

无节点的排队任务暂停会先确认任务没有运行锁，随后直接进入 PAUSED 并清除陈旧 `runId`、`sessionId`。无节点且已暂停的任务停止会在同一事务中按 `taskId + runId` 释放匹配锁、结束该 run，并保留该 run 的既有命令预算。恢复要求 SSH 任务已完成暂停、`desiredState=PAUSED` 且不再归属节点；状态不满足时返回 `409`。

资源删除与启动、暂停或恢复使用同一资源文档的控制声明写入来产生事务冲突。删除先提交时，后到控制请求返回 `409`；控制先提交时，删除扫尾会写入 STOPPED、标记 `resourceDeleted` 并取消不再适用的 PENDING 操作。资源删除不会删除既有日志。

## 命令与模板

`POST /api/v1/command-templates` 创建命令模板，`GET/PATCH/DELETE /api/v1/command-templates/{templateId}` 管理模板。向正在采集的任务发送手动命令：

```http
POST /api/v1/tasks/{taskId}/commands
Idempotency-Key: 5141e0ef-e2be-4a7d-8475-8137eb58c784
Content-Type: application/json

{"command":"show status","newline":"\n","timeoutSeconds":30}
```

新手动命令必须绑定有效的运行与会话，初始化未完成、断连、暂停或缺少会话身份时返回 409。排队配额按当前运行、当前会话的手动命令统计，达到 100 条时返回 429；旧会话积压不占新会话配额。节点按当前会话 FIFO 分发，每周期最多取消 100 条已取得身份快照的旧排队记录；不会把旧命令迁移至重连后的连接。任务归属检查、并发配额准入、命令入队、幂等成功映射及操作审计在同一 MongoDB 事务内提交。

相同操作者、幂等键和请求内容重试时返回原命令，即使任务已经停止也不重新入队；重放仍需通过当前权限检查。同键不同内容返回 409。事务提交确认丢失时仅查询原命令，无法确认则返回 503，调用方须使用相同幂等键重试。设备 socket 发送不属于数据库事务；历史 PENDING 映射若已有原命令仅重放原记录，不补发或补造历史审计。

提交响应中的命令 `id` 可用于查询处理结果；任务级执行记录同时包含手动和定时命令：

```http
GET /api/v1/commands/{commandId}
GET /api/v1/tasks/{taskId}/command-executions?page=1&pageSize=50
```

PSH 调试失败只影响当次命令，不停止日志采集。后续定时或手动 `debug`、重连后的初始化 `debug` 均可独立执行；单次握手失败不重复提交口令。采集器先发送 Ctrl-C，确认新的普通提示符且无 Password 提示后才用 `ls` 恢复命令通道。恢复未确认时普通命令不会写入设备，后续 `debug` 可重新尝试恢复再执行。已阻断的定时普通命令不占用预算，也不生成执行记录；已开始的定时 `debug` 失败保留已占用的一次预算，记录为 `FAILED`。

## 后台配置与节点

以下接口要求 `admin` 作用域。平台设置的保留期是数据库中的版本化配置；修改必须携带当前版本，避免两个管理员互相覆盖。节点登记与 worker 心跳分离：登记不会启动 worker，也不会把节点标记为在线。

```http
GET /api/v1/platform-settings
PATCH /api/v1/platform-settings
Content-Type: application/json

{"retentionDays":14,"version":1}
```

`retentionDays` 范围为 1 到 3650 天。节点配置使用以下接口：

```http
GET  /api/v1/admin/nodes
POST /api/v1/admin/nodes
PATCH /api/v1/admin/nodes/{nodeId}
```

登记请求包含 `id`、`url`、`capacity` 和可选的 `accepting`。`url` 支持 HTTP 和 HTTPS，包括 Docker 服务名、内网 IPv4/IPv6；不得包含用户信息、查询参数、片段、附加路径或通配监听地址。公布地址须从 API 所在容器或主机可达，且与同 ID worker 的上报地址一致。更新节点仅接受 `version`、`capacity` 和 `accepting`，当前版本不提供已登记节点地址修改接口。登记响应和节点列表会同时给出人工配置与 worker 的实际心跳，例如 `registered`、`online`、`reportedAt`、`reportedUrl` 与 `urlMismatch`；以心跳为准判断在线状态。未登记但有心跳的节点以 `registered=false`、`version=0` 返回，沿用原部署配置；登记后才由平台容量和准入设置约束。实际容量取平台配置、本机 `NODE_CAPACITY` 和 100 的最小值。登记不会启动 worker，也不能把不同 ID 的心跳合并。

## 服务账号与审计

`DELETE /api/v1/admin/nodes/{nodeId}?version={version}` 软删除节点；发现项版本为 0，已登记项携带当前版本。有采集归属、未结束运行或活动连接时返回 409。删除与调度领取在事务中串行化，配置删除、禁用准入和审计共同提交；不删除日志、文件目录和历史节点地址，也不停止操作系统中的 worker 服务。后续心跳不恢复该节点，管理员可用 POST 显式重新登记；旧版本删除请求不能删除恢复后的节点。

服务账号接口也要求 `admin`。创建响应中的 `token` 只返回这一次；后续列表只返回名称、作用域、任务范围、撤销状态和过期时间，不会返回明文或散列。创建、撤销与操作审计共同提交，事务确认丢失时只读核对原凭据或撤销状态。重复撤销不追加成功状态审计，每次 HTTP 调用仍记录请求事件。明文响应完全丢失后无法从列表恢复，应核对并撤销该令牌后重新创建。

```http
POST /api/v1/service-tokens
Content-Type: application/json

{"name":"reporting","scopes":["logs:read"],"taskIds":["task-example"],"expiresInDays":30}

GET    /api/v1/service-tokens?page=1&pageSize=20
DELETE /api/v1/service-tokens/{tokenId}
```

可用作用域为 `admin`、`tasks:read`、`tasks:write`、`tasks:control`、`commands:send`、`templates:read`、`templates:write`、`logs:read` 与 `logs:download`。`taskIds` 省略时不按任务白名单限制；提供后，任务级接口只允许访问列出的任务。

管理员可查询操作审计、运行事件和请求排障事件。三个接口均支持 `page`（从 1 开始）和 `pageSize`（最多 100）：

```http
GET /api/v1/audit-events?action=control%3ARUNNING&actor=admin&taskId=task-example&start=2026-09-08T00:00:00%2B00:00&end=2026-09-09T00:00:00%2B00:00
GET /api/v1/runtime-events?taskId=task-example&nodeId=node-a&type=CONNECTION_GAP&level=WARNING&outcome=UNKNOWN&start=2026-09-08T00:00:00%2B00:00&end=2026-09-09T00:00:00%2B00:00
GET /api/v1/request-events?method=POST&route=%2Fapi%2Fv1%2Ftasks&status=202&requestId=request-example&taskId=task-example
```

`audit-events` 可按 `action`、`actor`、`taskId`（审计目标）、`level`、`outcome`、`requestId` 和时间范围筛选；`runtime-events` 可按 `taskId`、`nodeId`、`type`、`level`、`outcome`、`requestId` 和时间范围筛选。`request-events` 可按 `method`、`route`、`status`（HTTP 状态）、`level`、`outcome`、`requestId`、`clientIp`、`taskId` 和时间范围筛选。

`level` 只能是 `DEBUG`、`INFO`、`WARNING` 或 `ERROR`；`outcome` 只能是 `PENDING`、`SUCCEEDED`、`FAILED`、`CANCELLED` 或 `UNKNOWN`，其他值返回 422。旧事件没有持久化这些字段时，服务端按与页面相同的规则在数据库聚合中推导后筛选和计数；分页完成后才批量补齐操作者、任务、资源、模板、节点、作业和命令名称。返回条目可包含 `summary`、`level`、`outcome`、`actorName`、`targetName`、`taskName`、`deviceIp`、`requestId`、`clientIp`、`reason`、`runId`、`sessionId` 和 `nodeId`。摘要为中文；例如 `CONNECTION_GAP` 显示为“采集连接中断”，其结果为 `UNKNOWN`、级别为 `WARNING`。

请求事件仅记录 `/api/v1/` 下的非 `GET`/`HEAD` 请求，或状态码不低于 400 的读取请求；健康检查和普通读取不会进入该集合。请求成功发送完毕时，HTTP 202 为 `PENDING`，其他 2xx 为 `SUCCEEDED`，4xx/5xx 为 `FAILED`；响应未完整发送时为 `UNKNOWN`。持久化有短暂超时且失败不会改变原请求结果；已取消请求不会在清理阶段等待写入。事件不保存 query、header、请求/响应 body 或设备日志正文，失败原因仅来自已脱敏的安全错误文本。`request_events` 以 `createdAt` 建立 30 天 TTL，另建 `requestId` 和 `(taskId, createdAt)` 索引；该 TTL 不作用于设备日志、下载或归档。

时间范围必须成对提供、带 UTC 时区或偏移、长度大于零且不超过 31 天。旧的连接缺口事件使用 `detectedAt`，接口会按其发生时间稳定排序，并返回统一的 `createdAt` 供显示。上述事件接口不读取设备日志正文或口令。

## 日志与导出

按小时查看目录：

```http
GET /api/v1/tasks/{taskId}/log-hours?page=1&pageSize=100
```

可选 `date=YYYY-MM-DD` 按 `Asia/Shanghai` 自然日筛选。一个小时可包含重连或回拨形成的多个片段；响应的 `fragmentCount`、`readyCount`、`openCount` 与 `unavailableCount` 给出覆盖状态，`files` 按运行、会话和首块序号排序。每个片段含逻辑文件 `id`、状态、字节数、归档名、可用的 `sha256` 与序号范围；内部路径、密码和令牌不会返回。

小时 `status` 为 `READY`、`OPEN` 或 `UNAVAILABLE`。`integrity` 为 `VERIFIED`（所有 READY 片段有摘要）、`UNVERIFIED`（已封存但缺少摘要）、`OPEN`（仍在写入）或 `UNAVAILABLE`（存在删除中或不可用片段）。它描述目录中的片段状态，不替代下载作业的最终清单校验。

读取开放原始文件的一段内容：

```http
GET /api/v1/log-files/{fileId}/content?offset=0&limit=65536
```

响应中的 `data` 是 Base64，`nextOffset` 可用于下一次读取。开放文件和已归档文件均可读取；gzip 归档定位可能需要顺序解压，响应字节仍保持落盘顺序。

成功响应可包含标准 `Server-Timing` 头，所有 `dur` 数值单位为毫秒。`catalog` 是节点文件目录查询，
`path` 是路径及水位校验，`queue` 是进入专用读取线程前的等待，`io` 是线程内文件操作
（包含文件打开、归档解压及读取限速等待），`encode` 是节点 Base64 和 JSON 响应编码。
API 还附加 `api_catalog`（文件目录查询与权限检查）、`api_node`（节点地址查询）、
`api_upstream`（上游 HTTP 调用，包含节点执行、连接池等待及最多一次只读重试）和
`api_decode`（上游 JSON 解码）。`api_upstream` 包含节点阶段，不能重复相加。
这些阶段不包含全部鉴权、响应发送和客户端等待时间，不能将其和直接视为端到端延迟。
计时头不包含正文、路径或凭据，不影响原有 JSON 字段；客户端须兼容节点升级期间缺少该头。

对于当前采集会话的 OPEN 文件，节点在核对任务、运行和会话身份后，按内存中已确认写入的字节水位读取，不必等待秒级目录刷新。因此 `nextOffset` 可以超过最近一次小时目录响应中的 `bytes`。其他会话、历史文件及 READY 归档只使用登记水位，不按磁盘额外尾部推断写入成功。下载作业仍冻结创建时的文件清单和字节数，不因实时读取水位推进而扩大快照。

创建下载任务：

```http
POST /api/v1/downloads
Idempotency-Key: dfa82b76-09d8-4d96-a0e8-74f9c6e97a5d
Content-Type: application/json

{"taskId":"task-example","hourIds":["2026-09-08T00:00:00+00:00"],"allowPartial":false}
```

使用 `GET /api/v1/downloads/{jobId}` 查询状态；成功后从 `GET /api/v1/downloads/{jobId}/content` 下载，客户端可发送标准 `Range` 头继续未完成下载。`DELETE /api/v1/downloads/{jobId}` 取消队列或运行中的下载。

创建下载或检索时，冻结文件清单、文件保留期保护、作业入队、幂等成功映射和操作审计共同提交。范围无效或审计失败不会单独延长保护期或留下可执行作业。同键同内容重试返回原作业和原文件快照，不会再次扫描扩大范围；同键不同内容返回409，提交确认不明且无法只读确认时返回503，须使用相同幂等键重试。历史PENDING映射若已有作业仅返回原记录，不补建作业或补造审计。

取消状态与取消审计在同一事务提交；已取消或已结束的作业再次取消不会追加成功状态审计，每次调用仍记录请求事件。工作节点执行、压缩产物和HTTP下载传输不属于上述数据库事务。

工作节点的成功、失败、取消和过期终态也与对应审计共同提交。终态提交无法确认时保留作业执行槽及产物，按1秒至30秒指数退避，仅重试数据库收尾；已提交终态不重复记审计，取消竞争返回实际状态。进程关闭或崩溃可能遗留RUNNING状态和未确认产物，跨进程持久恢复尚待完善，不能将该状态视为成功或直接删除产物。

下载和搜索作业从 `progress=0` 开始，按冻结片段的字节权重推进；运行中最大为 99，只有 `status=SUCCEEDED` 时才写入 100。失败、过期或取消的作业不会被显示为完成。下载最多选择 168 个小时，预计归档大小超过 20 GB 会被拒绝；节点临时导出空间上限为 100 GB，成功产物和下载保护最长保留 24 小时。多片段导出使用 ZIP STORE；`allowPartial=false` 时任一不可用片段会拒绝或失败，设为 `true` 时结果清单会明确列出 `missing`。

浏览器原生下载可先创建短期会话：

```http
POST /api/v1/downloads/{jobId}/browser-session
```

响应返回同源 `url`，同时设置仅对该下载路径生效的 5 分钟 HttpOnly cookie。浏览器随后访问返回 URL；脚本客户端仍可带 Bearer Token 调用 content 接口。

票据摘要与授权审计在同一数据库事务提交，确认成功后才设置 Cookie。提交确认丢失时仅从多数确认的主库查询本次固定票据；无法确认则返回503且不设置 Cookie，可重新申请。请求取消不会吞掉取消异常。数据库不保存票据明文；整个HTTP响应丢失后重新申请可能产生另一张独立短期票据，旧票据自然到期。

字面量搜索最多覆盖 24 小时时间范围：

```http
POST /api/v1/log-searches
Idempotency-Key: c7a96caa-c5e1-467e-a2cc-b8776c8b4e5c
Content-Type: application/json

{"taskId":"task-example","keyword":"ERROR","start":"2026-09-08T00:00:00+00:00","end":"2026-09-08T01:00:00+00:00"}
```

通过 `GET /api/v1/log-searches/{jobId}/results?page=1&pageSize=100` 读取结果。每条记录包含 `fileId`、`offset`、`text`；`truncated=true` 表示已达到服务端匹配上限。

## 错误格式

实时接口为 `WS /api/v1/tasks/{taskId}/logs`。连接后十秒内发送首帧 `{"token":"<access-token>","cursor":null}`，后续消息包含 `fileId`、`sessionId`、`offset`、`endOffset`、Base64 `data` 及 `cursor`。重连传回最后游标；收到 `gap` 表示实时缓冲过期，需要从文件接口补读，原始文件并未因此丢失。服务端会持续检查令牌有效性。

错误响应使用统一结构：

```json
{"error":{"code":"422","message":"输入校验失败","requestId":"request-example"}}
```

保留响应头 `X-Request-ID`，便于与服务端日志关联。`401` 表示令牌无效，`403` 表示缺少权限，`409` 通常表示幂等键冲突、版本冲突或资源尚未就绪。
