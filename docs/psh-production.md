# PSH 生产解密部署

本说明用于公司网络中的 PSH 解密服务接入。系统默认 `PSH_MODE=disabled`；本机开发和自动测试只使用 `PSH_MODE=mock` 及 `MockTransport`，不会请求 OAuth 或 itapi。

## 配置位置

完整 Docker 部署由 `deploy-all.sh --init` 生成 `.env`，额外 Worker 由 `deploy-worker.sh --init` 生成 `.env.worker`。两种文件都会被 Compose 的 `env_file` 原样传给 Worker。原生部署生成 `native.env`，安装器将同一组值写入受限的 `worker.env` systemd `EnvironmentFile`。

生成文件默认关闭 PSH，并包含两个公开服务地址：

```dotenv
PSH_MODE=disabled
PSH_TOKEN_URL=https://hicode-auth-hz.hikvision.com/oauth/token
PSH_API_URL=https://itapi.hikvision.com/api/
PSH_REQUEST_TIMEOUT_SECONDS=4
PSH_TOTAL_TIMEOUT_SECONDS=9
```

公司部署使用受限 `.env` 或密钥管理系统填写以下字段，再将 `PSH_MODE` 改为 `http`。不要把这些值写入 `.env.example`、部署脚本、镜像、工单或服务日志。

```dotenv
PSH_MODE=http
PSH_CLIENT_ID=<OAuth client ID>
PSH_CLIENT_SECRET=<OAuth client secret>
PSH_API_KEY=<itapi API key>
PSH_USER_NAME=<itapi userName>
```

`PSH_TOKEN_URL` 和 `PSH_API_URL` 可以替换为公司 HTTPS 代理地址。URL 不能含用户名、口令或 token。配置变更后只重启对应 Worker；已有会话不会替换正在进行的调试握手。

## 请求流程

1. Worker 从设备响应中取得完整原始 Base64 `source`，不先解码或改写。
2. Worker 以 `grant_type=client_credentials`、`client_id` 和 `client_secret` 请求 OAuth token。
3. Worker 调用 itapi，设置 `X-HiCode-Authorization: Bearer <token>`、`X-CloudApi-ClientId` 和 `X-CloudApi-ApiKey`，请求 JSON 为 `{"source":"<raw>","userName":"<configured>"}`。
4. 仅响应 `data.data` 中的非空字符串可作为设备口令。业务码 `403003` 时仅刷新一次 token 并重放一次 itapi 请求。

token 缓存属于每个 `PshPasswordProvider` 实例；当前采集运行会创建一个实例，不能将其视为整个 Worker 的共享缓存。token、client secret、API key、解密服务响应正文和解密口令不会写入数据库、审计事件或普通服务日志。设备原始输出仍按采集合同保存，因此设备若在输出中回显挑战值或口令，该原始正文可能出现在日志文件中；部署人员须按设备输出和现有日志访问控制处理，不能把本节当作对原始采集正文的脱敏承诺。远端请求失败、超时或再次 `403003` 时，本次 `debug` 失败；设备端不会自动重发或猜测口令。

## 超时与验收

默认单请求为 4 秒、整个解密流程为 9 秒，适合保守的本机和低延迟环境。参考工具使用 30 秒单请求；公司网络确有更长链路时可将 `PSH_REQUEST_TIMEOUT_SECONDS` 设到最多 30、`PSH_TOTAL_TIMEOUT_SECONDS` 设到最多 120，且总超时不得小于单请求超时。

`debug` 是任务命令队列中的一个整体步骤。生产任务的 `debug` 条目还必须将 `timeoutSeconds` 设为不小于 `PSH_TOTAL_TIMEOUT_SECONDS`，并预留 PSH/ASH 探测和恢复时间；否则命令队列会先取消该步骤。资源监控使用同一入口，但当前 `ensure_ash_for_monitor()` 的固定队列预算为 10 秒；即使提高 `PSH_TOTAL_TIMEOUT_SECONDS`，监控触发的 PSH 切换仍会在 10 秒处超时。需要较长链路时，应先用显式 `debug` 完成切换，或在变更监控上层预算并补齐回归验证后再启用监控触发切换。先在 `PSH_MODE=mock` 和隔离账号上验证命令顺序，再由公司网络执行真实 OAuth/itapi 连通性验收。
