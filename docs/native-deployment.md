# Ubuntu/Debian 原生部署

本方案在 Ubuntu 或 Debian 主机上直接安装运行依赖，不创建 Docker 容器、不拉取镜像。它要求主机可以访问系统软件源、MongoDB 8 官方 APT 源及 Python 托管下载源，并由 `root` 或具备 `sudo` 权限的运维账号执行。部署后的 API、采集节点、Nginx 与 MongoDB 都由 systemd 管理并设置为开机启动。

原生部署的数据库是启用认证的单成员 `rs0` 副本集。它提供 MongoDB 事务所需的副本集能力，但单机故障会同时影响全部副本，不构成高可用或灾备方案。需要多成员容灾时使用 [Docker 独立数据库部署](deployment.md) 或另行建设 MongoDB 集群。

## 快速开始

在仓库根目录执行完整单机安装。省略组件参数等同于 `all`：

```sh
sudo bash ./deploy-native.sh all --config /etc/camera-logs/native.env
```

首次执行先由脚本独占创建配置，再按组织的密码与备份规范编辑。已存在的配置文件不会被 `--init` 覆盖：

```sh
sudo bash ./deploy-native.sh all --config /etc/camera-logs/native.env --init
sudo editor /etc/camera-logs/native.env
sudo chmod 0600 /etc/camera-logs/native.env
sudo bash ./deploy-native.sh all --config /etc/camera-logs/native.env
```

部署脚本会在 `INSTALL_ROOT/venvs/backend`、`worker` 和 `database` 分别建立 Python 3.12 虚拟环境；主机没有 Python 3.12 时，使用 `uv` 下载隔离运行时，不替换系统 Python。受限网络可按组织镜像策略预设 `UV_PYTHON_INSTALL_MIRROR`。它还会配置原生 MongoDB 8 单成员副本集、独立 Nginx 和相应 systemd 服务。安装完成后以 systemd 查看状态和日志：

```sh
sudo systemctl status camera-logs-mongo camera-logs-api camera-logs-worker camera-logs-frontend
sudo journalctl -u camera-logs-api -u camera-logs-worker -f
```

首次空数据库会根据 `ADMIN_USERNAME` 与 `ADMIN_PASSWORD` 创建管理员。默认账号为 `admin`，默认初始密码为 `asdf!234`；首次登录必须修改密码。初始化只作用于空数据库，重跑脚本不会覆盖已改的管理员密码、配置、数据库或设备日志。

## 配置文件

`--config` 必须使用绝对路径。所有 `*_ROOT` 路径也必须是无空格的非根绝对路径。配置文件包含密码、MongoDB URI 和服务间令牌，不能放入仓库、网页根目录或普通用户家目录。以下变量描述原生部署的路径、端口和跨组件合同：

| 变量 | 用途 |
| --- | --- |
| `INSTALL_ROOT` | 程序、Python 3.12 虚拟环境、前端构建产物和生成服务配置的安装根目录。 |
| `SERVICE_USER` | API、worker、MongoDB 与独立 Nginx 使用的专用非 root 系统账号；不可使用人工登录账号或 `root`。 |
| `PYTHON_BIN`、`INSTALL_PACKAGES` | 优先使用的 Python 3.12+ 路径及自动安装开关。`INSTALL_PACKAGES=true` 会安装系统依赖、MongoDB 8 官方源与受管 Python；预装依赖时可设为 `false`。 |
| `DATA_ROOT` | systemd 工作目录及 Nginx 临时目录根；不要与其它应用、系统根目录或临时目录共用。 |
| `LOG_ROOT` | worker 的设备日志、小时归档与分卷日志根目录。 |
| `API_LOG_ROOT` | API 运行日志及导出临时文件目录，应与 `LOG_ROOT` 分开规划。 |
| `MONGO_DATA_ROOT` | 原生 MongoDB 数据目录，必须位于可备份的本地磁盘，不能与其它 Mongo 实例共用；默认可作为 `DATA_ROOT` 下的专用子目录。 |
| `MONGO_BIND_IP`、`MONGO_ADVERTISED_HOST`、`MONGO_PORT` | MongoDB 监听地址、在副本集中公布的 IPv4/DNS 地址和端口。跨机访问时 `MONGO_BIND_IP` 必须保留 `127.0.0.1` 以完成本地初始化，并额外包含内网监听地址；初始副本集公布地址不可直接修改。 |
| `MONGO_ADMIN_USER`、`MONGO_ADMIN_PASSWORD`、`MONGO_REPLICA_KEY` | 首次空实例初始化使用的 MongoDB 管理账号、密码和成员认证密钥。已有实例重跑会认证并核对既有拓扑，拒绝覆盖未知管理员、密钥或数据。 |
| `MONGO_URI` | API 与 worker 连接认证副本集的 URI，必须包含正确数据库、凭据和 `replicaSet=rs0`。密码中的 URI 保留字符需要 URL 编码。 |
| `ENCRYPTION_KEY` | Fernet 密钥。后端与全部采集节点必须相同；更换后无法解密既有任务密码。 |
| `BOOTSTRAP_TOKEN`、`INTERNAL_TOKEN` | 平台服务间令牌。后端和所有采集节点必须共享同一组值。 |
| `API_PORT` | API 监听端口。跨机时配合实际监听地址、防火墙和反向代理配置。 |
| `NODE_ID`、`NODE_PORT`、`NODE_URL` | worker 稳定唯一 ID、内部 HTTP 端口及数据库中公布的可达地址。`NODE_URL` 的端口必须与 `NODE_PORT` 一致。 |
| `FRONTEND_PORT` | Nginx 对浏览器提供服务的端口。 |
| `BACKEND_UPSTREAM` | Nginx 访问 API 的可达 HTTP 地址；分机部署时不可填写前端自身的 `127.0.0.1`。 |
| `FORWARDED_ALLOW_IPS` | API 信任的反向代理来源 IP 或 CIDR。填写 Nginx 到 API 的真实来源，不接受浏览器自行伪造的 `X-Forwarded-For`。 |
| `RETENTION_DAYS` | 默认日志保留天数。已保存的平台保留期设置优先于该环境默认值。 |

`DATA_ROOT`、`LOG_ROOT`、`API_LOG_ROOT` 与 `MONGO_DATA_ROOT` 应明确各自职责；后三者可位于 `DATA_ROOT` 下，但不能被其它服务混用。受管安装重跑时会拒绝这些运行目录发生变化，不自动搬迁历史数据。迁移时先停止相关 systemd 服务，确认采集连接已经关闭，完整复制原目录并验证权限、容量和备份后再按迁移流程建立新的受管安装。不要通过清空目录来解决启动失败，也不要删除真实设备日志。

建议分别备份 `/etc/camera-logs/native.env`、`MONGO_DATA_ROOT`、`DATA_ROOT` 与 `LOG_ROOT`。恢复既有系统时必须恢复相同的 `ENCRYPTION_KEY` 和服务令牌；仅恢复数据库而生成新密钥会使历史设备密码无法解密。

## 组件部署

可只部署一个组件，默认仍读取同一份受保护配置：

```sh
sudo bash ./deploy-native-database.sh --config /etc/camera-logs/native.env
sudo bash ./deploy-native-backend.sh --config /etc/camera-logs/native.env
sudo bash ./deploy-native-worker.sh --config /etc/camera-logs/native.env
sudo bash ./deploy-native-frontend.sh --config /etc/camera-logs/native.env
```

也可以由总入口指定组件：

```sh
sudo bash ./deploy-native.sh database --config /etc/camera-logs/native.env
sudo bash ./deploy-native.sh backend --config /etc/camera-logs/native.env
sudo bash ./deploy-native.sh worker --config /etc/camera-logs/native.env
sudo bash ./deploy-native.sh frontend --config /etc/camera-logs/native.env
```

跨服务器部署时，数据库、后端和所有 worker 必须使用同一个 `MONGO_URI`、`ENCRYPTION_KEY`、`BOOTSTRAP_TOKEN` 与 `INTERNAL_TOKEN`。每个 worker 使用不同且稳定的节点 ID，并将 `NODE_URL` 写为后端可以访问的地址。前端的 `BACKEND_UPSTREAM` 指向后端实际可达地址；后端的 `FORWARDED_ALLOW_IPS` 只信任前端代理来源。组件入口只安装和重启对应的本机服务，外部依赖必须已经可达。

## 重跑、运维与限制

重复执行相同命令用于更新程序或修复 systemd/Nginx 配置。脚本保留已有配置、管理员密码、MongoDB 数据和日志；它不自动清理历史设备日志、不缩短保留期，也不重置服务间密钥。变更数据库凭据、加密密钥或节点公开地址前，应先进行备份并按停机迁移流程验证。

排障优先检查服务状态、端口监听、Nginx 到 API 的代理连通性及 MongoDB 副本集状态：

```sh
sudo systemctl --failed
sudo journalctl -u camera-logs-api -u camera-logs-worker --since '30 minutes ago'
sudo ss -lntp
```

自动安装支持 Ubuntu 22.04、Ubuntu 24.04 和 Debian 12 的 MongoDB 8 官方源。其它版本应预先安装兼容的 `mongod`，并设置 `INSTALL_PACKAGES=false`。前端构建需要 Node.js 18+、npm 和 Nginx；系统 Node 版本不足时，自动安装模式从 `nodejs.org` 下载隔离的 Node 22.18.0 并核验官方 SHA256，不替换系统 Node。也可以按组织的软件源策略预装 Node.js 18+。原生路径依赖主机软件源可用性和 systemd；离线主机、非 Ubuntu/Debian 发行版以及需要多节点高可用的场景不在本方案范围内。
