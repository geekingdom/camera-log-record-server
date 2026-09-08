# API 使用说明

所有公开接口以 `/api/v1` 开头，除健康检查外均使用 Bearer Token：

```http
Authorization: Bearer <access-token>
```

创建任务、模板、手动命令、搜索和下载作业要求 `Idempotency-Key`；编辑使用版本号，启停接口按期望状态幂等处理。以下值均为示例，不能作为真实凭据使用。

## 任务

创建采集任务：

```http
POST /api/v1/tasks
Idempotency-Key: 46a0f16d-8c5c-4b05-a9d4-2d8b972030bf
Content-Type: application/json

{
  "name": "机房摄像机 01",
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

## 命令与模板

`POST /api/v1/command-templates` 创建命令模板，`GET/PATCH/DELETE /api/v1/command-templates/{templateId}` 管理模板。向正在采集的任务发送手动命令：

```http
POST /api/v1/tasks/{taskId}/commands
Idempotency-Key: 5141e0ef-e2be-4a7d-8475-8137eb58c784
Content-Type: application/json

{"command":"show status","newline":"\n","timeoutSeconds":30}
```

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

登记请求包含 `id`、`url`、`capacity` 和可选的 `accepting`。`url` 仅允许 HTTPS，开发环境允许 `localhost` 或环回地址的 HTTP；不得包含用户信息、查询参数、片段或路径。更新节点仅接受 `version`、`capacity` 和 `accepting`，当前版本不提供已登记节点地址修改接口。节点列表会同时给出人工配置与 worker 的实际心跳，例如 `registered`、`online`、`reportedAt`、`reportedUrl` 与 `urlMismatch`；以心跳为准判断在线状态。未登记但有心跳的节点以 `registered=false`、`version=0` 返回，沿用原部署配置；登记后才由平台容量和准入设置约束。实际容量取平台配置、本机 `NODE_CAPACITY` 和 100 的最小值。

## 服务账号与审计

服务账号接口也要求 `admin`。创建响应中的 `token` 只返回这一次；后续列表只返回名称、作用域、任务范围、撤销状态和过期时间，不会返回明文或散列。撤销会立即使账号失效，但保留审计记录。

```http
POST /api/v1/service-tokens
Content-Type: application/json

{"name":"reporting","scopes":["logs:read"],"taskIds":["task-example"],"expiresInDays":30}

GET    /api/v1/service-tokens?page=1&pageSize=20
DELETE /api/v1/service-tokens/{tokenId}
```

可用作用域为 `admin`、`tasks:read`、`tasks:write`、`tasks:control`、`commands:send`、`templates:read`、`templates:write`、`logs:read` 与 `logs:download`。`taskIds` 省略时不按任务白名单限制；提供后，任务级接口只允许访问列出的任务。

管理员可查询操作审计和运行事件，两者均支持 `page`（从 1 开始）和 `pageSize`（最多 100）：

```http
GET /api/v1/audit-events?action=control%3ARUNNING&actor=admin&taskId=task-example&start=2026-09-08T00:00:00%2B00:00&end=2026-09-09T00:00:00%2B00:00
GET /api/v1/runtime-events?taskId=task-example&nodeId=node-a&type=CONNECTION_GAP&start=2026-09-08T00:00:00%2B00:00&end=2026-09-09T00:00:00%2B00:00
```

`audit-events` 可按 `action`、`actor` 和 `taskId`（审计目标）筛选；`runtime-events` 可按 `taskId`、`nodeId` 和 `type` 筛选。时间范围必须成对提供、带 UTC 时区或偏移、长度大于零且不超过 31 天。旧的连接缺口事件使用 `detectedAt`，接口会按其发生时间筛选和排序，并返回统一的 `createdAt` 供显示。上述事件接口不读取设备日志正文或口令。

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

创建下载任务：

```http
POST /api/v1/downloads
Idempotency-Key: dfa82b76-09d8-4d96-a0e8-74f9c6e97a5d
Content-Type: application/json

{"taskId":"task-example","hourIds":["2026-09-08T00:00:00+00:00"],"allowPartial":false}
```

使用 `GET /api/v1/downloads/{jobId}` 查询状态；成功后从 `GET /api/v1/downloads/{jobId}/content` 下载，客户端可发送标准 `Range` 头继续未完成下载。`DELETE /api/v1/downloads/{jobId}` 取消队列或运行中的下载。

下载和搜索作业从 `progress=0` 开始，按冻结片段的字节权重推进；运行中最大为 99，只有 `status=SUCCEEDED` 时才写入 100。失败、过期或取消的作业不会被显示为完成。下载最多选择 168 个小时，预计归档大小超过 20 GB 会被拒绝；节点临时导出空间上限为 100 GB，成功产物和下载保护最长保留 24 小时。多片段导出使用 ZIP STORE；`allowPartial=false` 时任一不可用片段会拒绝或失败，设为 `true` 时结果清单会明确列出 `missing`。

浏览器原生下载可先创建短期会话：

```http
POST /api/v1/downloads/{jobId}/browser-session
```

响应返回同源 `url`，同时设置仅对该下载路径生效的 5 分钟 HttpOnly cookie。浏览器随后访问返回 URL；脚本客户端仍可带 Bearer Token 调用 content 接口。

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
