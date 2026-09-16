# 请求对象与来源IP核对

## 对象修复

原请求事件只提取路径中的 `task_id`，节点、资源等路由的 `node_id`、`resource_id` 和 `identifier` 缺失；页面把集合查询没有设备IP也展示成未记录。

现在从已匹配路由模板提取对象类别、展示名称和范围；只有声明了允许的路径参数才保存实体ID。后台按已有分页结果批量补齐实体名称，前端集合请求显示“集合查询（无单个对象）”，平台设置显示“平台级接口”，实体优先显示名称与IP/ID。访问JSONL与请求事件采用相同对象提取。历史集合按路由只读推导，历史未保存的对象ID不猜测。

不新增集合、不增加请求写入频率，不读取请求正文、查询或密码，仍按30天TTL治理。新增字段不参与查询或排序，无需盲目添加索引；关联查询限制在当前页。

## 来源IP证据与待办

2026-09-16更正：此前从组件项目名推断用户独立部署是错误的。用户实际使用`deploy-all.sh`跨机模式，`deploy.sh`会拆分组件项目并逐一传递同一入口env_file。以下当时写到`.env.backend`的处理指引不适用于该现场，应修改原统一`.env`，仅重建API时同时指定DEPLOY_ENV_FILE与--env-file。用户无需改变完整部署入口。代理信任缺少172.21.0.2的实测事实仍有效。

用户报告浏览器10.41.203.12访问服务器10.41.203.43，事件记录172.21.0.2。用户先称完整部署，但随后提供docker ps，实际运行项目分别为：

- `camera-log-record-server-frontend`：`camera-log-record-server-frontend-frontend-1`
- `camera-log-record-server-backend`：`camera-log-record-server-backend-api-1`
- `camera-log-record-server-worker` 与 `camera-log-record-server-database`

以实际容器标签为准。用户随后用 `docker exec camera-log-record-server-backend-api-1 printenv FORWARDED_ALLOW_IPS` 实测返回 `127.0.0.1`。独立后端不信任实际对端172.21.0.2，Uvicorn忽略该代理的XFF，导致来源保持容器IP。应在实际`.env.backend`配置 `FORWARDED_ALLOW_IPS=127.0.0.1,172.21.0.2` 后仅重建API；完整Compose的API信任配置不同，不能套用其默认值说明此现场。

最初给出的Compose排障命令漏了项目名，出现`service api is not running`。这只是未定位到实际项目，不能作为API停机证据。之后已改为按实际容器名只读检查；排障命令不应输出完整Compose配置或全部环境变量，以免泄露凭据。

## 验证

- 对象提取与持久化、请求查询等专项21项通过，前端164项及生产构建通过，后端全量1500项通过。
- 隔离真实Cookie浏览器验证三类事件、失败请求、分页及新对象展示；1440/390截图已查看，无页面横向溢出，窄屏表格保留自身滚动。
- 第一次浏览器对象断言失败是新请求晚于页面初始查询截止时间，刷新“近24小时”后通过；不是服务端漏记。随机数据库和临时日志目录均已清理。
- 代理联合回归验证正式IP策略和持久化请求记录都采用10.41.203.12，非可信对端伪造XFF仍403并记录真实对端；无需为显示修改审计或白名单的IP读取来源。
- 隔离真实API/Worker/WebSocket与小时下载冒烟通过，随机库与临时日志清理完成。
- 公司代理配置原因已确认，但实际更新及新请求验收尚未执行，不把本机模拟代理头测试等同目标部署验收。
