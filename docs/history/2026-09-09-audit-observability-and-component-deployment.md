# 2026-09-09 审计排障与组件部署记录

## 范围

本记录覆盖工作区中尚未提交的审计排障、事件展示与独立组件部署增强。它不代表发布完成，也不替代当前状态总表。

## 审计与请求排障

- 业务审计自动关联请求编号和客户端 IP；请求上下文在请求结束时复位。
- 管理端审计、运行和请求事件均提供中文摘要、结果、级别、安全原因和关联对象名称。关联查询只投影用户、任务、资源、模板、节点、作业和命令的展示字段，不读取密码、散列、令牌、header、body 或设备日志正文。
- 请求排障事件只记录 `/api/v1/` 的写请求或失败读取请求。HTTP 202 显示为 `PENDING`，完整 2xx 为 `SUCCEEDED`，4xx/5xx 为 `FAILED`，未完整发送为 `UNKNOWN`；取消请求不在 finally 等待持久化，持久化超时或故障不会覆盖原响应或异常。
- `request_events` 使用 `createdAt` 的 30 天 TTL，另有 `requestId` 与 `(taskId, createdAt)` 索引。该 TTL 仅清理排障事件，不清理设备日志、下载文件或归档。
- 历史事件缺失 `level`、`outcome` 时，Mongo 聚合按与展示器一致的规则推导后筛选和计数。查询先在库内排序和分页，只有当前页最多 100 条进入关联名称补齐；无派生条件保持 `find/sort/skip/limit/count` 快路径。

## 本地证据

`scripts/verify_event_queries.py` 已针对随机 Mongo 数据库实际执行。它覆盖 `WARNING/UNKNOWN`、`ERROR/FAILED`、显式结果和级别优先、空 `debugError`、1000 条历史事件的派生筛选与第二页计数。脚本在 finally 删除随机数据库，并以数据库列表确认清理完成。

隔离浏览器验收使用随机数据库、临时 API/Vite 服务、临时日志目录和 Cookie 管理员会话。最终结果为 `passed=true`、`cookieOnly=true`、`tabs=3`、`requestConflict409=true`、`runtimeFixtureWithoutDevice=true`、`temporaryDatabaseDropped=true`、`temporaryLogDirectoryDropped=true`。运行事件展示了“采集连接中断”、任务“隔离连接演示任务”、IP `198.51.100.77`、中文“警告/未知”与脱敏安全原因；桌面、折叠侧栏、移动端和详情抽屉截图保存在本机 `output/playwright/audit-browser-f13321914728/`，不作为仓库产物提交。

本轮相关后端回归 30 项、前端测试 49 项、前端检查与构建、审计浏览器脚本语法检查、验证脚本 Ruff 和 `git diff --check` 均已通过。完整全仓验证与 Linux CI 结果应以提交后的独立运行记录为准。

## 本机 API 更新与 404 复验

旧 API PID 49854 的 OpenAPI 未包含 `/api/v1/request-events`。其最后一次相关访问有明确的本地服务日志证据：`.local/service-logs/api/camera-logs.jsonl:2318` 的请求编号 `6dd61839ad6b48f193c760193dbe5199` 于 2026-09-09 15:25:30 请求 `GET /api/v1/request-events`，返回 404，耗时 2.297ms。该旧日志仅用于定位，不导入数据库，也不修改真实业务数据。

随后受控更新 API 至 PID 79958。以管理员 Token 查询 `audit-events`、`runtime-events`、`request-events` 均返回 200；Chrome 刷新请求记录页后不再发生 404，空列表按预期显示。Worker PID 25871 未更新，任务 34/35 继续为 `COLLECTING`，其 run、session 和 generation=28 未改变。更新后本轮全量后端测试 685 项、前端测试 49 项与前端构建通过；真实 Mongo 的事件派生筛选和任务控制事务验证再次通过，并清理了各自随机验证数据库。

## 独立组件部署

独立数据库、后端、采集节点和前端部署现在以同一随机基名加组件后缀创建 Compose 项目，避免此前共享项目名导致 `storage-init` 服务名碰撞。`scripts/verify_component_deployment.py` 在 Linux 上将验证四组件独立启动、重复执行不改写环境文件、容器重启后的健康检查，以及容器、卷和临时挂载目录清理。

`component-smoke` 已配置在 CI 工作流，但当前工作区没有新提交对应的 Linux 运行结果。因此本记录只确认脚本和本地静态/单元证据，不能将独立组件部署描述为已在 Linux CI 通过或已发布。独立 Mongo 在 Linux host 网络下的官方初始化端口 27017 冲突已在 `deploy/mongo-host-user-init.sh` 与 `deploy/database.yml` 修复，部署相关两份测试文件共 43 项通过；仍须提交后由 Linux CI 实跑，组件部署验收才可完成。
