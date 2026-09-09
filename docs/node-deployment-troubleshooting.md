# 节点登记与部署排障

## 完整部署与独立节点的配置来源

| 入口 | 默认配置 | 节点身份和地址 | 网络 |
| --- | --- | --- | --- |
| `bash deploy-all.sh` | `.env` | `COLLECTOR_NODE_ID=compose-worker-1`、`COLLECTOR_NODE_URL=http://worker:8001` | API 和 worker 位于同一 Docker 网络，使用服务名互访 |
| `bash deploy-worker.sh` | `.env.worker` | `NODE_ID`、`NODE_URL`、`NODE_PORT` | Linux host 网络，需要 API 可达的节点内网地址 |
| `sudo bash deploy-native-worker.sh --config /etc/camera-logs/native.env` | 指定原生配置 | `NODE_ID`、`NODE_URL`、`NODE_PORT` | 主机网络；同机可用环回地址，跨机使用内网地址 |

完整部署已启动一个 worker，不会自动加载 `.env.worker`。后台登记是管理已有或待部署节点的准入配置，不会创建容器、启动进程或改变现有 worker 的身份。节点按 ID 合并心跳，不能把 `compose-worker-1` 的心跳归入 `collector-01`。

完整 Docker 部署中 `http://worker:8001` 是正确的节点地址。`127.0.0.1` 从 API 容器看指向 API 容器自身，不能代替 worker。完整部署的 worker 默认也没有发布宿主机 8001 端口，不能只把登记地址换成宿主机 IP 就认为可达。保留已有节点 ID 与地址，避免影响历史日志归属。

## 保存时报 HTTP 校验错误

旧版本的登记校验只允许环回 HTTP，与 Docker 默认 `http://worker:8001` 及独立节点内网 HTTP 配置矛盾。新版已支持 HTTP(S) 服务名和 IPv4/IPv6，同时拒绝凭据、附加路径、查询参数及通配监听地址。

单独修复 HTTP 登记只需更新 API 和前端；本次同时交付节点删除，需一并更新 worker。以下按默认项目名、根目录 `.env` 执行；如原部署指定了不同项目名或配置路径，须使用原值：

```bash
git pull --ff-only
docker compose --env-file .env --project-name camera-log-record-server --file deploy/docker-compose.yml up -d --build --no-deps api frontend worker
```

更新 worker 时采集连接会受控释放并重连。刷新浏览器，在已发现的 `compose-worker-1` 行点击登记，保留 `http://worker:8001`。随后可编辑容量和准入开关。误登记的 `collector-01` 若没有实际同名进程，会继续显示离线；可点击删除并二次确认，不需要更改实际 worker 身份来迁就该记录。

删除采用软删除，已有日志和历史节点地址保留；有采集归属、未结束运行或连接尚未释放时拒绝删除。后台删除不会卸载或停止系统服务；持续心跳也不会自动恢复已删除节点。需恢复时可用相同 ID 显式重新登记并填写原实际地址。

## 独立节点始终离线

1. 核对配置文件拼写：默认是 `.env.worker`，不是 `.env.woeker`。自定义文件必须显式传入 `--env-file`。
2. 核对是否实际执行 `bash deploy-worker.sh`；仅编辑配置或网页登记不启动服务。
3. `ENCRYPTION_KEY`、`BOOTSTRAP_TOKEN`、`INTERNAL_TOKEN` 必须填写原平台对应值；`MONGO_URI` 和 `DATABASE_NAME` 必须指向 API 使用的同一数据库。脚本不会合并另一份 `.env` 中的凭据。如果这些值只是展示时脱敏，以服务器实际内容为准。
4. `NODE_URL` 是纯文本，例如 `NODE_URL=http://10.41.203.43:8001`，不能写成 Markdown 链接。该地址须从 API 容器或主机可达。
5. 完整部署的 MongoDB 默认仅容器内部可达，`mongo1` 等名字不是独立 host 网络的可用连接地址。扩容须先规划可达且具有认证的数据库拓扑；不要为临时连通直接对外暴露无认证数据库。

只读取非敏感配置和状态的诊断命令：

```bash
# 完整部署的实际容器状态
docker compose --env-file .env --project-name camera-log-record-server --file deploy/docker-compose.yml ps

# 实际 worker 身份、地址与数据库名，不输出密钥或数据库 URI
docker compose --env-file .env --project-name camera-log-record-server --file deploy/docker-compose.yml exec -T worker python -c 'from camera_logs.common.config import Settings; s=Settings(); print({"nodeId":s.node_id,"nodeUrl":s.node_url,"database":s.database_name})'

# 独立节点的状态；只在确实使用独立入口时执行
docker compose --env-file .env.worker --project-name camera-log-record-server-worker --file deploy/worker.yml ps
docker compose --env-file .env.worker --project-name camera-log-record-server-worker --file deploy/worker.yml logs --tail 100 worker
```

## 存储目录

`HOST_LOG_ROOT` 是采集节点的数据根目录，`API_DATA_ROOT` 是 API 的运行日志和临时文件目录，应使用不同路径。两者指向同一目录会让不同进程共享运行日志文件及其轮转，影响排障完整性。

已有数据时不能只改 `HOST_LOG_ROOT`，否则历史目录可能无法找到。需受控停止对应服务、确认无活跃文件写入后迁移并保留目录结构。修复节点 HTTP 登记本身不要求移动现有日志，不应为此删除目录或重建数据卷。
