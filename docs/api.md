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

## 日志与导出

按小时查看目录：

```http
GET /api/v1/tasks/{taskId}/log-hours?page=1&pageSize=100
```

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

使用 `POST /api/v1/service-tokens` 创建服务账号令牌，使用 `DELETE /api/v1/service-tokens/{id}` 撤销。支持 `tasks:read`、`tasks:write`、`tasks:control`、`commands:send`、`logs:read`、`logs:download`、`templates:read`、`templates:write` 等权限，并可通过 `taskIds` 限定任务范围。创建或撤销令牌需要 `admin` 权限。

错误响应使用统一结构：

```json
{"error":{"code":"422","message":"输入校验失败","requestId":"request-example"}}
```

保留响应头 `X-Request-ID`，便于与服务端日志关联。`401` 表示令牌无效，`403` 表示缺少权限，`409` 通常表示幂等键冲突、版本冲突或资源尚未就绪。
