# 登录与访问控制

## 平台用户

内置管理员用户名默认 `admin`，开发与正式初始密码按用户要求为 `asdf!234`。仅当数据库不存在内置管理员时初始化，重复部署或修改 `.env` 不覆盖已修改的密码。首次登录强制改密，普通新密码与子账户密码为 12～128 字符。管理员不能通过账号列表被删除、禁用或重置，自身通过顶栏修改密码。

管理员可创建、编辑、停用和软删除子账户，设置功能权限、全部资源或指定资源。用户名规范为小写且不可重复使用；密码经随机盐 PBKDF2-SHA256 保存，不回传明文/哈希。创建和重置的子账户首次必须改密。禁用、删除、权限变更及重置密码递增会话版本，使旧登录即时失效；用户自行改密仅保留新签发的当前会话。

浏览器通过 HttpOnly、SameSite=Strict Cookie 登录，默认八小时到期。写请求需 `X-Requested-With: XMLHttpRequest`，Origin 存在时必须同源；HTTPS 自动 Secure，也可设置 `SESSION_COOKIE_SECURE=true`。`X-Auth-Required: true` 表示平台会话失效，设备 HTTP 认证自身返回 401 不触发平台退出。

第三方继续使用 Bearer 服务 Token。Bootstrap Token 仍用于部署和既有自动化，不作为网页登录入口。登录接口有限次预算，五分钟内每账号最多十次、每来源最多一百次尝试，成功登录也计次。

## 权限

| 权限 | 操作 |
| --- | --- |
| `tasks:read` | 查看授权设备资源、任务与命令记录 |
| `resources:create` | 新增设备资源与新资源认证预览 |
| `resources:write` | 编辑、删除资源与该资源认证预览 |
| `tasks:create` | 新增日志采集任务 |
| `tasks:write` | 编辑采集任务 |
| `tasks:control` | 启停、暂停、恢复；删除资源同时需要该权限 |
| `logs:read` | 实时与历史日志、搜索 |
| `logs:download` | 创建导出和下载 |
| `commands:send` | 手动设备命令 |
| `templates:read` / `templates:write` | 全局共享命令模板的读取/维护 |
| `admin` | 平台设置、节点、账号、IP 策略、审计管理 |

子账户不能获得 `admin` 或 `*`；管理员身份与业务功能权限不等价，IP 规则仍可限制管理员的业务操作。旧第三方 Token 的 `tasks:write` 原来包含创建资源/任务，鉴权时展开为新的权限后再与 IP 权限求交集，保持既有接口合同；新建子账户必须明确分配独立权限。

资源范围 `null` 表示全部、`[]` 表示没有资源。任务的主资源及非空串口服务器资源必须全部获授权；日志、命令、下载、操作记录均从所属任务校验。指定范围的账户不能创建或探测新资源，可在授权资源下创建任务。权限撤销后禁止新的读取、命令和下载，实时订阅下一次推送重新鉴权。已发出的设备命令不回滚，已提交的后台导出/搜索按作业流程完成，但结果读取仍检查最新权限。网页登录下载必须保有有效平台会话，五分钟下载票据不独立延长已退出的会话。

## 客户端 IP 策略

白名单只限制访问和操作平台的浏览器/第三方客户端来源 IP，不匹配设备资源 IP、采集目标或串口服务器 IP，不安装到 worker 的连接或调度逻辑，也不会因目标设备不在白名单而停止任务。

默认关闭。管理员在账号管理的“IP 访问控制”中配置多条单 IP 或 CIDR，支持 IPv4/IPv6。启用后未命中来源在登录前即返回 403；匹配规则权限取并集，再与账号权限取交集，不能提升账号权限。`*` 代表该规则不再收窄账号权限。规则保存使用版本比较，并拒绝让当前来源失去管理权限的配置。

白名单应用于 `/api/v1/` 和实时 WebSocket；健康检查及无数据的静态登录页面不受限。客户端地址只读 ASGI `request.client.host`。Compose 的 API 不发布宿主机端口，Nginx 覆盖而非追加客户端传来的 X-Forwarded-For，并保留 Host 端口供同源校验。额外反向代理必须配置准确的可信来源链，不能直接相信客户端自带转发头；TLS 示例直接将 `/api/` 代理到 API，避免双层代理把真实来源改成内部代理地址。

地址变化造成无法管理时，服务器管理员可在受控终端恢复。下面先执行预览，加 `--apply` 才关闭策略并记录审计，不影响采集连接：

```sh
docker compose --project-name camera-log-record-server --env-file .env -f deploy/docker-compose.yml exec -T api python - --allow-configured-database < scripts/reset_ip_policy.py
docker compose --project-name camera-log-record-server --env-file .env -f deploy/docker-compose.yml exec -T api python - --allow-configured-database --apply < scripts/reset_ip_policy.py
```

## 接口

`POST /api/v1/auth/login`、`GET /auth/me`、`POST /auth/logout`、`POST /auth/password` 负责登录与自身密码。管理员通过 `/api/v1/users` 创建/分页，`PATCH /users/{id}` 带版本编辑，`POST /users/{id}/reset-password` 重置，`DELETE /users/{id}?version=N` 软删除；`GET /users/permissions` 提供可分配权限。

`GET/PATCH /api/v1/admin/ip-policy` 获取或整体替换 `{version, enabled, rules:[{label,network,scopes}]}`；响应附当前 `clientIp`。服务端完整 OpenAPI 为最终接口定义。
