# Linux 一键部署

在仓库根目录执行：

```sh
./deploy.sh
```

脚本仅在 Linux 运行。Ubuntu 和 Debian 缺少 Docker Engine 或 Compose 插件时，会使用 Docker 官方 APT 源安装所需组件；其他发行版需要预先安装可用的 `docker compose`。

首次运行会独占创建根目录 `.env`，权限为 `0600`。其中包含 Fernet 加密密钥、服务令牌和初始管理员账号：用户名固定为 `admin`，初始密码为 `asdf!234`，脚本不会输出密码或令牌。首次登录必须改为至少 12 位的新密码；已部署数据库中的管理员密码不会被后续 `.env` 默认值覆盖。随后按组织要求轮换管理员凭据和服务令牌。

默认会话有效期为 8 小时。`SESSION_COOKIE_SECURE=false` 适用于 HTTP 初始部署；在 HTTPS 请求下服务会自动使用 Secure Cookie。生产 HTTPS 部署可在首次启动前或完成凭据轮换后按站点策略更新 `.env`。

重复执行是幂等的：已有 `.env`、MongoDB、API 与 worker 卷都会保留，Compose 项目名默认为 `camera-log-record-server`。多套部署可设置 `COMPOSE_PROJECT_NAME`，例如：

```sh
COMPOSE_PROJECT_NAME=camera-log-staging ./deploy.sh
```

当 `.env` 缺失但检测到同项目 Compose 卷时，脚本会拒绝启动。恢复应从原主机备份取回相同 `.env`，而不是生成新密钥，否则既有加密密码和令牌无法恢复。

部署会构建并启动三成员 MongoDB 副本集、API、worker 和前端。长期运行服务使用 `unless-stopped` 重启策略及 Docker 本地日志轮转。启动完成前脚本会等待 MongoDB、API、前端健康检查与一分钟内 worker 心跳；任一项失败将以非零状态退出。诊断时可运行：

```sh
docker compose --env-file .env --project-name camera-log-record-server --file deploy/docker-compose.yml logs --tail 150
```
