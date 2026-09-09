# 旧阶段验证记录

从当前总表迁出的历史阶段流水。以下PID、任务状态、测试数量只代表各次记录时点，不作为当前运行事实。

## 下一步顺序

最终功能提交 `2c62fdb`：Linux CI 后端 567 项、前端 25 项测试通过，Ruff、前端构建、构建上下文检查及 4 项副本集初始化检查通过；一键部署、真实代理账户权限与多路归档链路通过。具体证据见 [账户与部署记录](history/2026-09-09-access-and-deployment.md)。清理历史见 [清理记录](history/2026-09-09-status-and-cleanup.md)。未重跑的实体设备/集群验证不计入本轮通过项。

当前命令恢复与模块化提交 `d157cc2`：本地后端 573 项、前端 28 项测试、构建、Ruff 与模拟浏览器通过；默认十秒重连/实际暂停补强后再次 2 项通过。[Linux CI 34306652015](https://github.com/geekingdom/camera-log-record-server/actions/runs/34306652015) 四个作业全部成功。历史及真实 worker 尚未重启的部署边界见 [本轮记录](history/2026-09-09-command-recovery-and-modularity.md)。

1. R21：受限服务端清理器已实现；保护到期后重新核验并清理两份合成报告归档。已完成的 R23/R24/R25 不再列为待实现。
2. R16/R17/R12：已知实时范围与独立补读窗口、隔离模拟全链路已验证；继续无边界缺口定位和实体 debug 恢复，不重复开发范围阅读器。
3. R10/R11/R26：手动准入和领取事务已部署本机并完成两路实机发送验证，继续操作审计一致性和接管隔离；不重复开发已完成预算事务。
4. R20：对齐指标后安排集群全天验收，性能诊断与产品验收同步推进。

本轮访问边界与确认交互的验证和限制见 [访问审计与确认记录](history/2026-09-09-access-audit-and-confirmations.md)。新增平台 IP 规则不进入设备认证、采集连接或调度目标校验；受限客户端操作仍须具备对应账户权限。

功能提交 `7bd1fc0` 已通过 [Linux CI 34308408063](https://github.com/geekingdom/camera-log-record-server/actions/runs/34308408063) 的后端、前端、中文提交和容器集成四个作业；本地 577 项后端测试、32 项前端测试及最终相关 22 项回归通过。此证据覆盖 R16/R25/R26 本轮改动，不代表剩余项目验收完成。

R21 本轮后端全量 592 项通过，随后新增脚本集成 6 项通过；最终清理相关 34 项、前端 32 项及构建、Ruff 通过。两份本机报告的预览和执行入口均实际返回 PROTECTED，没有删除受保护归档。证据与重试条件见 [开发服务端清理记录](history/2026-09-09-development-archive-cleanup.md)。

R10 手动命令实现阶段后端全量 610 项、前端 32 项及构建、Ruff 通过；真实副本集并发准入、停止冲突、取消回滚和未知提交验证通过。新增模块分别为 39/77 行，runtime 469 行、collector 492 行。历史测试见 [手动命令事务记录](history/2026-09-09-manual-command-transactions.md)。当前真实 Worker 已更新至代码基线 `7b4964e`，两路恢复与手动发送及 R25 本轮 10 项回归证据见 [本机部署核对](history/2026-09-09-local-rollout-and-ip-boundary.md)。

R17 功能提交 `17747f9` 本地后端 610 项、前端 42 项、生产构建与 Ruff 通过，独立复核未发现阻塞问题；[Linux CI 34314414779](https://github.com/geekingdom/camera-log-record-server/actions/runs/34314414779) 四个作业全部成功。详细边界、浏览器与真实 API 证据及临时数据清理见 [实时范围补读记录](history/2026-09-09-live-range-recovery.md)。

R18/R26 功能提交 `19bdb74` 本地后端全量 623 项、前端 47 项、生产构建、Ruff 和 diff 检查通过；[Linux CI 34316605024](https://github.com/geekingdom/camera-log-record-server/actions/runs/34316605024) 后端、前端、中文提交、容器集成四个作业全部成功，包含新增真实审计事务和多路归档回归。隔离真实 Cookie 浏览器通过，临时服务/数据库/日志目录已回收。独立审查发现的夹具适配遗漏已修正，同名冲突回滚由真实数据库证明。本机 API 已更新为 PID 43955，Worker 25871 未变；34/35 维持原运行/会话/generation=28，日志继续增长。真实用户浏览器已刷新，当前需重新登录；未改真实管理员密码。详情见 [审计会话与事务记录](history/2026-09-09-audit-session-and-transactions.md)。

管理配置功能提交 `87a49da` 本地后端全量 627 项、前端 47 项、构建、Ruff 与 diff 检查通过；[Linux CI 34318069115](https://github.com/geekingdom/camera-log-record-server/actions/runs/34318069115) 四个作业全部成功，含新增正式管理 API 的真实事务及来源切换检查。验证覆盖冷启动默认记录回滚、节点登记/修改回滚、并发 CAS 唯一成功、IP 匹配集合失败不变化/成功后切换。临时数据库和目录均清理，额外核验并删除验证器早期错误派生的 18,846 字节服务日志。本机 API 已更新为 PID 49854，管理查询均为 200；Worker 25871 和 34/35 原运行/会话/generation=28 不变，日志继续增长。下一步为任务控制意图与审计事务，其余项目缺口继续保留。范围与后续见 [管理配置事务记录](history/2026-09-09-admin-audit-transactions.md)。
