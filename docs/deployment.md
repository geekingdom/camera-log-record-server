# Linux 一键部署

本文说明 Docker 部署入口。Ubuntu/Debian 主机如需直接安装依赖、不使用 Docker，请查看 [原生部署](native-deployment.md)。

## 五个部署入口

| 脚本 | 实际部署内容 | 默认配置文件 | 可自定义示例 |
| --- | --- | --- | --- |
| `./deploy-all.sh`（兼容 `./deploy.sh`） | 前端、API/调度器、采集节点、三成员 MongoDB | `.env` | `deploy/config/all.env.example` |
| `./deploy-frontend.sh` | Nginx 静态前端、同源 API/WebSocket 代理 | `.env.frontend` | `deploy/config/frontend.env.example` |
| `./deploy-backend.sh` | API、登录服务、调度器 | `.env.backend` | `deploy/config/backend.env.example` |
| `./deploy-worker.sh` | SSH/Telnet 采集、小时归档、节点内部接口 | `.env.worker` | `deploy/config/worker.env.example` |
| `./deploy-database.sh` | 同一服务器的三个 MongoDB 成员、认证、初始化 | `.env.database` | `deploy/config/database.env.example` |

所有入口支持 `--help`、`--init` 和 `--env-file /绝对路径/配置.env`。`--init` 只生成配置，不安装或启动服务；文件已存在时拒绝覆盖。配置不作为 shell 执行。独立组件默认采用不同 Compose 项目名，不会隐式启动其它组件，也不会移除其它服务。

完整单机部署可以直接执行 `./deploy-all.sh`。需要指定磁盘路径时，先运行 `./deploy-all.sh --init`，按示例修改 `.env` 后再部署。独立部署必须先填入外部依赖：

```sh
# 分别在数据库、后端、采集节点、前端服务器执行对应入口。
./deploy-database.sh --init
# 编辑 .env.database：跨机使用实际内网IP替换127.0.0.1，设置三个独立数据目录。
./deploy-database.sh

./deploy-backend.sh --init
# 编辑 .env.backend：填写MONGO_URI、API监听地址和可信前端代理来源。
./deploy-backend.sh

./deploy-worker.sh --init
# 编辑 .env.worker：复制后端的数据库及三项共享密钥，设置唯一节点ID与可达URL。
./deploy-worker.sh

./deploy-frontend.sh --init
# 编辑 .env.frontend：将BACKEND_UPSTREAM指向后端服务器的实际地址。
./deploy-frontend.sh
```

首次单独数据库部署会随机生成管理员密码及成员认证密钥。API 和采集节点的 `MONGO_URI` 使用 `.env.database` 中的数据库用户名、密码及三成员地址，格式参见后端配置示例；密码中的 URI 保留字符必须进行 URL 编码。数据库使用固定副本集名 `rs0`，并启用认证。三个成员部署在同一主机，可提供事务与副本一致性，但不代表多主机容灾。

完整单机入口保留既有的内部 Compose 网络数据库配置，不发布 MongoDB 宿主机端口，未启用数据库用户认证；不要把该网络开放给非平台容器。上文带认证的数据库配置用于独立部署入口。需要从其它主机访问数据库时使用独立数据库入口，不能直接给完整入口的无认证 MongoDB 增加端口发布。

`ENCRYPTION_KEY`、`BOOTSTRAP_TOKEN`、`INTERNAL_TOKEN` 在同一平台的后端与所有采集节点之间必须一致。独立节点配置故意不自动生成另一套密钥。若连接已有数据库，后端也必须使用原平台密钥。首次生成的新密钥仅适用于新平台，不能解密原平台任务密码。

独立后端默认只监听 `127.0.0.1:8000`；分机部署将 `API_BIND_IP` 改为内网 IP 或 `0.0.0.0`。`FORWARDED_ALLOW_IPS` 填前端代理实际来源 IP/CIDR，支持逗号分隔。前端容器中的 `127.0.0.1` 指向自身，`BACKEND_UPSTREAM` 应使用后端可达地址。平台白名单限制浏览器/API 客户端来源，不限制设备或串口目标。

## 自定义日志与数据目录

| 配置项 | 默认值 | 修改示例与含义 |
| --- | --- | --- |
| `HOST_LOG_ROOT` | `worker-data` 命名卷 | `/srv/camera-logs/collector-01`，采集节点的日志、归档和临时文件根目录 |
| `API_DATA_ROOT` | `api-data` 命名卷 | `/srv/camera-logs/api`，后端服务日志和临时文件根目录 |
| `MONGO_DATA_1/2/3` | `mongo1-data` 等命名卷 | `/srv/camera-logs/mongo1`、`mongo2`、`mongo3`；独立数据库的三个目录必须不同 |
| `RETENTION_DAYS` | `7` | 默认日志保留天数；已保存的后台配置优先于环境默认值 |
| `FRONTEND_PORT` | `5173` | 平台 HTTP 对外访问端口 |
| `API_PORT` | `8000` | 独立后端监听端口 |
| `NODE_PORT` | `8001` | 独立节点监听端口，须与公布的 `NODE_URL` 一致 |
| `NODE_ID` | 独立节点必填 | 同一平台稳定唯一，例如 `collector-01` |
| `NODE_CAPACITY` | `100` | 节点采集容量默认值；按磁盘与实际压测能力调整 |

容器内统一使用 `/var/lib/camera-logs`：`data/` 保存设备日志、小时归档及作业数据，`service-logs/` 保存服务运行日志。指定宿主机目录后，例如 `HOST_LOG_ROOT=/srv/camera-logs/collector-01`，设备数据位于 `/srv/camera-logs/collector-01/data/`。脚本只为固定目录设置应用 UID/GID `10001:10001`，不会递归改写已有日志。不要将 `/`、其它服务目录或三个数据库成员的共用目录设为数据根。

改变已有节点的存储路径不会自动搬迁历史数据。应先停止对应节点并确认连接关闭，完整迁移原目录，再使用相同节点 ID 和新路径启动；直接指向空目录会使已有日志目录记录无法找到文件。密码、数据库卷、节点日志和 `.env` 应按运维流程备份。

## 重启自动恢复

所有常驻 Docker 服务使用 `restart: unless-stopped`：进程异常退出及宿主机重启后会自动恢复。部署脚本在 systemd Linux 上执行 `systemctl enable --now docker`，确保 Docker daemon 本身开机启动。普通账号执行该步骤需要 sudo 权限。

`key-init`、每个数据卷的管理员预初始化作业和 `mongo-init` 都是一次性作业，成功后退出，不反复重启。首次独立数据库部署时，三个管理员预初始化作业处在各自隔离的 Compose 网络命名空间中，分别在容器本地 `127.0.0.1:27017` 写入管理员；之后才启动使用 host 网络及自定义端口的成员，避免官方 Mongo 镜像首次初始化争用宿主机 `27017`。数据目录非空时预初始化作业直接退出，不修改既有认证、数据或副本集，也不会触碰正在运行成员的数据锁。手动执行 `docker stop` 停止的容器依据 `unless-stopped` 语义保持停止，需要手动启动或再次运行部署脚本。脚本不会自动重启管理员明确停掉的任务；容器启动后的采集行为仍由任务期望状态决定。

## 健康检查与重复部署

完整部署等待数据库、API、前端和节点心跳。独立数据库检查一主两从及初始化退出码；独立后端检查数据库 ping；独立采集节点同时检查 HTTP 接口和一分钟内数据库心跳；独立前端同时检查静态页面及真实 API 代理链路。失败均返回非零退出码。可用 `DEPLOY_HEALTH_TIMEOUT=300` 修改等待秒数。

重复执行保留配置和数据。独立组件更新只影响它自己的 Compose 服务，不执行 `down --volumes` 或 `--remove-orphans`。诊断使用对应的项目名、配置和 Compose 文件，例如：

```sh
docker compose --env-file .env.worker --project-name camera-log-record-server-worker --file deploy/worker.yml logs --tail 100
```

完整部署的详细兼容说明如下。

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
