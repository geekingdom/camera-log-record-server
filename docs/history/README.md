# 历史记录入口

- [节点管理与小时归档时区](2026-09-09-node-management-and-timezone.md)：HTTP节点登记、删除竞争、浏览器确认与时区证据。
- [服务令牌审计事务](2026-09-09-service-token-audit.md)：创建撤销审计原子性与确认不明恢复。
- [手动命令提交审计](2026-09-09-manual-submission-audit.md)：入队、幂等映射、审计事务与停止后重放。
- [任务编辑与会话事务](2026-09-09-edit-and-session-transactions.md)：编辑、登录、退出、改密的数据库原子性与并发撤销验证。

历史报告只代表当时样本与条件。当前状态统一见 [当前状态总表](../implementation-status.md)。

- [旧状态快照](implementation-status-before-2026-09-09.md)：包含已过时描述，仅用于追溯。
- [2026-09-09 总表与清理核对](2026-09-09-status-and-cleanup.md)：清理数量、测试结果及服务端保留锁边界。
- [2026-09-09 账户与部署核对](2026-09-09-access-and-deployment.md)：来源 IP 边界、账户权限、一键部署与验收证据。
- [2026-09-09 命令恢复与模块化](2026-09-09-command-recovery-and-modularity.md)：普通命令恢复、连接生命周期、会话模块拆分和开发产物来源核验。
- [2026-09-09 来源访问、认证审计与命令确认](2026-09-09-access-audit-and-confirmations.md)：来源拒绝记录、凭据脱敏、设备目标边界和草稿删除确认。
- [2026-09-09 开发服务端归档受限清理](2026-09-09-development-archive-cleanup.md)：报告来源、实际正文核验、有限范围清理及下载保护边界。
- [2026-09-09 手动命令并发准入与领取](2026-09-09-manual-command-transactions.md)：有界队列事务、归属领取、排队守卫和真实副本集故障验证。
- [2026-09-09 本机服务更新与来源 IP 边界](2026-09-09-local-rollout-and-ip-boundary.md)：34/35 恢复、手动发送、日志增长与来源 IP 回归。
- [2026-09-09 实时省略范围与补读](2026-09-09-live-range-recovery.md)：字节范围状态机、分页阅读、并发隔离、浏览器及临时数据清理。
- [2026-09-09 审计页会话与业务事务](2026-09-09-audit-session-and-transactions.md)：Cookie 请求修复、真实浏览器验证、资源模板账户审计事务及范围边界。
- [2026-09-09 管理配置审计事务](2026-09-09-admin-audit-transactions.md)：保留期、节点与 IP 策略正式接口的失败回滚、并发 CAS 及后续控制事务边界。
- [2026-09-09 任务控制审计事务](2026-09-09-task-control-transactions.md)：控制操作、资源删除竞争、真实副本集回滚与 Worker/自动启动边界。
- [2026-09-09 审计排障与组件部署](2026-09-09-audit-observability-and-component-deployment.md)：请求排障事件、派生筛选、隔离浏览器证据、组件项目名隔离及待运行的 Linux CI 边界。
- [历轮验证记录](../validation.md)：保留既有路径，避免破坏引用。
- [HTTP 延迟诊断](../http-latency-diagnostics.md)：连接池与阶段延迟实验。
- [服务压测](../service-benchmark.md) 和 [调度压测](../scheduler-benchmark.md)：操作说明和历史样本，不代替集群验收。

新实验记录放在本目录。采集正文、压缩包及下载副本不入库。
# 原生部署

- [2026-09-09 原生主机部署](2026-09-09-native-deployment.md)
