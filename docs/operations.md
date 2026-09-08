# 部署与运维

## 本地或测试环境启动

```sh
cp .env.example .env
# 编辑 .env，填写实际 Fernet 密钥、BOOTSTRAP_TOKEN 和不同的 INTERNAL_TOKEN
docker compose -f deploy/docker-compose.yml up --build
```

前端默认发布到 `http://localhost:5173`，其 `/api/` 请求保留完整路径代理到 API，`/api/v1/tasks/{id}/logs` 保持 WebSocket Upgrade 头并禁用代理缓冲。容器 API 使用 `http://api:8000/health` 健康检查。

Compose 默认使用容器内三成员 MongoDB 地址，不读取本机开发环境的 `MONGO_URI` 覆盖该地址。需要外置数据库时显式设置 `COMPOSE_MONGO_URI`。将已确认的设备公钥文件通过 `KNOWN_HOSTS_FILE` 挂载，不能用示例空文件代替主机身份校验。生产 HTTPS 和节点内部 TLS 由实际基础设施终止与管理。

默认 Compose 的 worker 使用 `compose-worker-1`、`http://worker:8001` 和独立命名卷，重建容器时保持节点身份与日志。可以使用 `COLLECTOR_NODE_ID` 和 `COLLECTOR_NODE_URL` 显式覆盖。该配置只运行一个 worker，不能直接通过 `--scale worker` 让多个实例共享同一身份和日志卷。

多机扩容使用每台 Linux 主机的独立配置，先为该节点配置唯一的 `NODE_ID`、API 可访问的 `NODE_URL`、外部副本集 `MONGO_URI` 和已验证的 `KNOWN_HOSTS_FILE`。首次部署空日志目录时授予容器 UID/GID 10001 写权限：

```sh
sudo install -d -o 10001 -g 10001 -m 0750 /srv/camera-logs
docker compose -f deploy/worker-node.yml up -d --build
```

`HOST_LOG_ROOT` 默认 `/srv/camera-logs`，宿主机绑定目录不会继承镜像目录权限；已有日志目录应先检查所有权和挂载权限，避免无差别递归更改。多节点必须各自使用独立的持久磁盘路径与节点标识。

构建上下文使用 `.dockerignore` 排除 `.env`、本地凭据、虚拟环境、依赖目录、设备日志及下载产物。镜像仍需访问 Python、Node、Nginx 基础镜像仓库及相应包仓库；上下文排除不能替代运行时密钥管理。

## CI 容器链路验证

GitHub Actions 的 `container-smoke` 在独立 Linux runner 上生成一次性密钥，构建并启动三成员 MongoDB、API、worker 和 Nginx 前端。验证程序在 runner 上建立合成 Telnet 源，worker 经 Docker 网桥网关连接，所有控制和下载请求经过前端 `/api/` 代理。该流程不访问真实设备、不使用仓库密钥，结束后清理独立项目的容器与卷，失败时保留控制台诊断日志。

`scripts/prepare_container_env.py` 只用于隔离验收目录，默认独占创建 `.env`，权限为 0600；已有文件时立即失败，不能覆盖开发配置。容器链路结果以当前提交的 `container-smoke` 作业为准；少量合成日志的冒烟验证不能替代实体设备兼容性、500 路持续吞吐或长期容量验收。

## 生产 TLS

`deploy/nginx/tls-site.conf.example` 是外层 Nginx 的配置示例。将 `logs.example.com`、证书路径和上游网络改为实际值，并使用受信任 CA 签发与续期的证书。示例不提供自签名证书，也不应把测试证书作为生产 TLS 配置。

## 副本集检查与恢复

初始化仅在 MongoDB 返回 NotYetInitialized（94）时执行 `rs.initiate`，其他异常直接失败。脚本最多等待 120 秒，确认一主两从健康后才允许 API 和 worker 启动；已有副本集不会被重复初始化或重配置。

```sh
docker compose -f deploy/docker-compose.yml exec mongo1 mongosh --quiet --eval 'rs.status()'
docker compose -f deploy/docker-compose.yml exec mongo1 mongosh --quiet --eval 'db.adminCommand({ping:1})'
```

先恢复网络、磁盘和原有副本集成员，再让 MongoDB 按多数派选主。不要为了快速恢复执行 `rs.reconfig(..., {force: true})`，也不要删除数据卷后重建副本集；这些操作会造成已确认写入丢失或分叉。磁盘空间不足时先停止对应 worker 的新写入，保留证据，清理经确认可删除的轮转文件或扩容磁盘；不要删除 MongoDB 数据卷来释放空间。

恢复前记录 `rs.status()`、可用空间、受影响的 `NODE_ID` 和时间范围。恢复后检查 API 健康状态、worker 注册记录、目标节点 `LOG_ROOT` 的文件可读性，并用少量真实查询验证路由。

## 会话与下载

SSH 任务支持 `pause` 与 `resume`；暂停时 worker 停止当前会话，恢复会重新进入调度。Telnet 设备和串口任务不支持暂停。collector 默认在 10 秒没有收到日志时关闭连接，运行时按重连策略建立新会话。初始化命令按数组顺序逐条发送，不能依赖以分号拼接多条命令。

`register_local_devices.py` 可读取本地 SSH 或 Telnet 串口任务清单；注册幂等键包含协议、IP 和端口，同一串口服务器的不同端口不会冲突。`verify_live_service.py --name-prefix 联调串口-` 可限定验证串口联调任务，默认仍只验证 SSH 联调任务；加 `--stop-after` 会经正式 API 停止所选任务，使用前应确认前缀范围。

下载作业成功后，浏览器可请求 `POST /api/v1/downloads/{jobId}/browser-session`。服务为该作业写入 5 分钟的 HttpOnly、SameSite=Strict cookie，并返回同源下载 URL。API、worker、归档、作业及维护失败均进入结构化模块日志；访问记录包含 request ID，日志输出会对密码、令牌和授权信息脱敏。

## 日志时间前缀

设备输出进入 collector 后，每个 LF 或 CRLF 逻辑行都会以 `[YYYY-MM-DD HH:MM:SS] ` 开头。时间取服务器在 Asia/Shanghai 时区记录的该接收块首字节到达时间，不取设备时钟，也不会因写盘、归档或 WebSocket 转发延迟而改变。跨网络分包的同一行仅保留一个前缀，空行同样带前缀；原有正文和换行符保持不变。实时 WebSocket、小时原始文件、归档与下载读取的是同一份加前缀字节，排障时应按此前缀解释服务器接收时间。

## 归档命名

小时原始文件、旁路索引和压缩归档使用相同的可读 stem：`任务名-设备IP-开始时间-结束时间-run短ID-session短ID-part序号`，其中开始和结束是 Asia/Shanghai 自然小时边界，时间格式为 `YYYYMMDDHHMMSS`。任务名最长保留 80 个 UTF-8 字节，路径字符、控制字符和 IPv6 冒号会被安全替换；不要依赖未经处理的设备名称恢复原始配置。每次重连或同小时重新打开分段都会增加 `part` 序号，历史 `part-*.log` 与 `part-*.tar.gz` 也计入序号扫描。单个归档下载和 ZIP 中的条目保留该可读文件名；ZIP 遇到重名时添加序号而不覆盖条目。

## WebSocket 基准

压测脚本连接真实端点 `/api/v1/tasks/{taskId}/logs`，从本地 `.env` 读取 `BOOTSTRAP_TOKEN` 并在首帧发送鉴权 JSON。它按 `(fileId, sessionId)` 分组校验后续 `offset` 是否等于前一帧 `endOffset`，并验证 Base64 正文、`size` 和 `endOffset`；`gap`、协议错误、偏移错误或客户端异常都会使脚本以退出码 2 结束。多个客户端可同时订阅同一任务，但指标仅表示 API 到订阅端的实时 WebSocket 传输，不表示设备产生日志的吞吐。

```sh
python -m pip install websockets
python scripts/benchmark_websocket.py --task-id <taskId> --clients 500 --frames 1200 --timeout 300
```

使用非默认服务地址时传入 `--base-url ws://host:8000`；只有在调试明确的完整端点时才使用 `--url` 覆盖 URL 拼接。不要在参数、Shell 历史、日志或压测结果中传入或记录令牌。

`scripts/benchmark_collector.py` 的本地摘要比对配置可扩展到 500 路、每路 1,200 行/秒、总计 3,000,000 行，原始输入约 768 MB。由于日志行现已写入服务器时间前缀，改造前得到的 3,000,000 行原始 SHA-256 摘要和报告不适用于当前实现，也不能作为本版本的容量或一致性验收证据。后续基准必须使用加前缀后的期望字节重新生成摘要和报告。该配置用于 Collector/HourlyWriter 的本地一致性验证，不是端到端吞吐报告，也不是 24 小时稳定性或生产容量验收。Docker 镜像未在本仓库验证阶段构建。

## 归档恢复与异常隔离补充

节点维护会幂等续做本节点 DELETING 状态的删除；物理文件已不存在时继续清理目录记录。删除失败保持 DELETING 并记录错误时间，避免把已经删除的文件重新暴露为 READY。

节点维护会修复已发布 `.tar.gz` 对应的缺失目录项或 OPEN 目录项。恢复前流式验证日志摘要与字节数；包含索引摘要的新归档也验证索引。OPEN 项必须与清单中的任务、运行、会话及本节点身份一致，DELETING 等其他状态不会被恢复覆盖。校验失败保留归档并记录错误，运维应检查文件及故障事件，不能强行登记为 READY。

未设置 `retainUntil` 的归档正常参与默认七天保留期清理。删除前原子声明 DELETING，并重新检查下载保护时间，避免扫描后新增保护的文件被删除。数据库持续失联时逐路关闭采集实例；某一路关闭失败不会阻止其他连接释放。未确认关闭的实例保留，释放失败的任务标记 BLOCKED，相关待完成操作标记 FAILED，必须确认连接和日志状态后处理。

## 请求完成与下载异常日志

API 访问日志在应用响应结束后写入，包含 `responseComplete` 和 `responseBytes`。字节数表示 ASGI 发送调用已成功返回的正文大小，不代表客户端已经保存到磁盘。下载已发送 200 响应头但随后失败时，保留真实的 200 状态，同时记录异常和 `responseComplete=false`；排障不能仅按 HTTP 状态判断成功。响应头尚未发出的取消记为 499，其他未处理异常记为 500。401、422 等已完整发送的错误响应仍为 `responseComplete=true`，该字段只表示传输完整性。

## Telnet 协议保活

Telnet 设备和 Telnet 串口共用连接适配器，使用 `send_iac(IAC + NOP)` 发送两字节协议保活。`telnetlib3.iac()` 仅适用于 DO、DONT、WILL、WONT 协商，不能用于 NOP，否则会因参数异常关闭连接。保活失败写入运行日志并关闭连接，交由运行实例重连。对端协议控制流量不算设备正文，不能重置默认十秒无日志看门狗；停止连接会同时取消保活协程。
