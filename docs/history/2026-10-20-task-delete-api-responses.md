# 任务软删除、API响应释义与常用命令管理

## 目标和交付约束

本轮包含五项：每个接口完整返回字段释义；任务软删除及资源删除级联；资源任务数与看板活跃数排除软删；创建人独立列；常用命令分页、命令/描述模糊查询、状态筛选和批量启禁/删除。沿用中文提交、推送、等待最终完整CI，不自动部署生产。

## 实现中的合同

任务DELETE使用当前version和tasks:write/tasks:control及创建者或管理员权限。删除写STOPPED意图，收尾由持有连接的Worker完成；不清理files/jobs和物理日志，原有下载制品按原保留期可用。列表默认排除软删，includeDeleted=true查看历史；历史任务不再显示编辑和生命周期按钮。资源删除级联主设备和串口服务器关联任务。

常用命令默认20条、可选50/100；query搜索命令和描述，enabled按状态筛选。批量操作最多100条，版本冲突导致整批回滚；操作和审计、幂等结果共同提交。

API响应字段由实际路由返回合同维护，包含嵌套数组、状态条件、统一错误及非JSON协议，禁止用单一示例冒充完整字段清单或用“当前字段”泛化说明。

## API 文档同源机制

请求说明迁入实际 Pydantic Field、Query、Path；响应合同位于 `common/api_contracts`，安装为 FastAPI 运行时响应模型并生成 OpenAPI。文档不再维护业务字段说明字典，只展开实际 OpenAPI。后续新接口可直接声明 `response_model`，无需登记文档字段。WebSocket 的首帧 token/cursor 及 data/gap/status 共用实际接收、发送模型和模型 JSON Schema。全局错误处理器也通过共享错误模型构造实际 JSON 正文。

模型拒绝额外字段、无效类型或缺失必填字段；历史记录可省略的字段不伪造默认值，可空与可省略分别声明。自动生成测试使用未登记的新接口证明请求/响应字段、类型、必填性及说明会直接更新目录，且真实错误返回触发校验。字段漏写、空白或仅英文说明也会拒绝文档生成。自动化只能保证结构与声明一致，中文业务解释仍经逐域源码复核，不能声称能自动判断业务语义。

本轮纠正的具体语义包括初始化命令写入后的额外等待、定时命令的首次间隔与独立预算、Core 的 OBSERVING/CHANGING/STABLE 稳定条件、HTTP 事件发送完整性、节点心跳和容量含义、各路由具体 ID 来源，以及时间范围包含/排除边界与默认北京时间。维护规则已写入工程 AGENTS.md。

## 当前验证

- 任务页面桌面1440与移动390浏览器通过：创建人独立列、取消与确认删除、默认隐藏、包含删除重新显示、不可再编辑或启动，无页面溢出。
- 真实隔离API/Worker/WebSocket/小时下载通过；仅删除本脚本创建合成任务后，Worker完成停止并释放节点归属，旧匿名下载URL仍206返回原16字节。
- 最终隔离验收的工具执行输出确认随机库 `isolated_browser_smoke_d89d30d09b634faf90ea052c5d5e1cf0` 通过：`passed=true`、`realWorker=true`、`realWebSocket=true`、`browserHourDownload=true`，并生成 `live-desktop.png`、`archives-mobile.png`、`logs-workspace-desktop.png`、`archives-workspace-mobile.png`；输出还确认 `temporaryDatabaseDropped=true`、`temporaryLogDirectoryDropped=true`。先前 `/tmp/camera-oct20-real-smoke-dynamic.log` 是失败诊断日志，不作为通过证据；成功子进程临时日志按隔离脚本约定回收。
- 上述隔离脚本随机数据库与模拟日志已自动回收；其余本轮专项验证继续进行，最终统一回收自建服务。

最终本机后端2057项、Ruff、前端201项与构建通过；真实Mongo任务软删除和常用命令事务、Explain通过。自建服务与随机库按来源清理。

首轮功能提交 `d175482` 的 CI 39422760181 六项成功、三项失败，未视为交付成功。修复节点维护/关闭字段与任务命令领取版本的严格合同遗漏，并更新旧模板导航及批量任务浏览器夹具。修复后全套27项生产浏览器、真实跨Worker SSH恢复通过。额外双节点迁移和单节点回退均保持最大活动连接数1；macOS目录别名按resolve后验证仍不能跨节点或越出根目录。修复提交继续等待完整CI。
