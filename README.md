# Camera Log Record Server

摄像机日志采集与检索服务。后端为 Python 3.12、FastAPI 和 MongoDB 副本集；前端为 Vue 3 与 Vite。

服务采集 SSH、Telnet 设备与 Telnet 串口日志，按会话和小时保存原始内容及归档。每个逻辑日志行在接收时均以前缀 `[YYYY-MM-DD HH:MM:SS] ` 记录服务器的 Asia/Shanghai 首字节接收时间；存储文件和实时日志使用同一份加前缀内容。SSH 任务可暂停和恢复；Telnet 任务不提供暂停。默认连续 10 秒没有收到日志时，collector 关闭当前会话并进入重连流程。SSH 使用协议 keepalive，Telnet 使用 IAC NOP 与 TCP keepalive。

## 开发

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

接口、浏览器下载会话、任务控制和日志检索示例见 [docs/api.md](docs/api.md)。运行时以 JSONL 写入 API/worker 的模块日志、操作记录及异常追踪；日志字段会脱敏密码、令牌和授权头。

开发时使用 CodeGraph 先同步索引，再评估会话运行时的变更影响：

```sh
codegraph sync .
codegraph impact SessionRuntime
```

当前验证证据见 [docs/validation.md](docs/validation.md)。
