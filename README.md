# Camera Log Record Server

摄像机日志采集与检索服务。后端为 Python 3.12、FastAPI 和 MongoDB 副本集；前端为 Vue 3 与 Vite。

开发与交接先查看 [当前状态总表](docs/implementation-status.md)，区分最终需求、实现位置、验证证据和未完成项。实验历史入口见 [历史记录](docs/history/README.md)。开发产物清理见 [清理说明](docs/development-cleanup.md)。

控制台默认从设备资源开始：海康网络设备经 Digest（默认）或 Basic 的 ISAPI 认证后保存型号、短序列号与软件版本；串口服务器仅需名称和 IP。资源下可创建多路采集任务，海康设备支持三种采集协议，串口服务器支持 Telnet 串口。海康的串口任务既可选择已有服务器，也可自定义连接地址。同一端口允许多个任务；任务的采集凭据独立于资源的 HTTP 凭据。详见 [设备资源与目录说明](docs/device-resources.md)。

服务采集 SSH、Telnet 设备与 Telnet 串口日志，按会话和小时保存原始内容及归档。每个逻辑日志行在接收时均以前缀 `[YYYY-MM-DD HH:MM:SS] ` 记录服务器的 Asia/Shanghai 首字节接收时间；存储文件和实时日志使用同一份加前缀内容。SSH 任务可暂停和恢复；Telnet 任务不提供暂停。默认连续 10 秒没有收到日志时，collector 关闭当前会话并进入重连流程。SSH 使用协议 keepalive，Telnet 使用 IAC NOP 与 TCP keepalive。

控制台把实时打印、小时归档与历史检索放在独立日志工作台中，按任务选择后再建立订阅。小时目录可以按上海自然日筛选，并展示同一小时内的运行/会话片段、原始与归档大小、片段可用性和完整性汇总；实时缓冲出现缺口时可从片段字节读取接口补读。

管理员控制台还提供平台保留期、节点登记与准入、第三方服务账号、审计记录和运行事件。节点登记是持久化的人工配置，不会启动 worker 或伪造在线心跳；服务账号明文只在创建时返回一次，数据库和列表响应均不保存或返回明文。详见 [docs/api.md](docs/api.md) 与 [docs/operations.md](docs/operations.md)。

## 开发

Linux 完整部署执行 `./deploy-all.sh`（兼容 `./deploy.sh`）；也提供 `deploy-frontend.sh`、`deploy-backend.sh`、`deploy-worker.sh`、`deploy-database.sh` 四个独立部署入口。每个脚本支持 `--init` 生成配置，日志路径、端口、保留天数、分机地址和 Docker 开机自启动详见 [一键部署](docs/deployment.md) 与 `deploy/config/*.env.example`。首次管理员为 `admin`，初始密码 `asdf!234`，登录后必须修改；重复部署不会覆盖已修改密码。用户、资源范围及客户端 IP 权限见 [登录与访问控制](docs/user-access.md)。

```sh
python -m pip install '.[test]'
uvicorn camera_logs.main:app --host 0.0.0.0 --port 8000
python -m camera_logs.worker
```

前端：

```sh
cd frontend
npm ci
npm run dev -- --host 0.0.0.0 --port 5173
npm run build
```

部署、环境变量、TLS、副本集恢复和基准压测见 [docs/operations.md](docs/operations.md)，组件职责与数据路由见 [docs/architecture.md](docs/architecture.md)。提交前可运行 `scripts/install-git-hooks.sh` 启用中文提交标题及“背景、变更、测试、影响”正文模板。

接口、浏览器下载会话、任务控制、后台管理和日志检索示例见 [docs/api.md](docs/api.md)。运行时以 JSONL 写入 API/worker 的模块日志、操作记录及异常追踪；日志字段会脱敏密码、令牌和授权头。

初始化、定时或手动命令中的独立 `debug` 支持 PSH 到 ASH 自动握手，已经确认 ASH 时跳过。完整 Base64 密文作为解密接口 `source` 原样提交，本地可使用离线 Mock 口令；配置、串口连续打印处理和失败语义见 [docs/psh-debug.md](docs/psh-debug.md)。

开发时使用 CodeGraph 先同步索引，再评估会话运行时的变更影响：

```sh
codegraph sync .
codegraph impact SessionRuntime
```

当前验证证据见 [docs/validation.md](docs/validation.md)。

经正式 API 驱动的多路真实 Telnet 压测、小时包流式摘要比对及报告判定见 [docs/service-benchmark.md](docs/service-benchmark.md)。该工具独立记录吞吐、并发重叠窗口和清理结果，不用进程内合成写入结果替代服务级验收。
