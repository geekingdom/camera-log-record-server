# MongoDB 增长型记录保留策略

设备日志正文、小时归档和 NFS coredump 源文件保存在 Worker 文件系统，不进入 MongoDB；MongoDB 保存任务配置、文件目录、认证历史和排障元数据。认证历史、审计事件、运行事件、命令执行记录和操作记录会随运行时间增长，因此按集合分别治理，避免一个全局 TTL 误删仍用于恢复的记录。

## 默认行为

所有增长型业务记录默认永久保留。`Settings` 中的 `*_retention_days=0` 表示不生成清理计划。管理员未显式配置期限时，认证记录和审计记录不会被静默删除；请求排障事件已沿用独立的固定 30 天 TTL，以避免高频 HTTP 访问记录无界增长。

## 可配置项

| 配置项 | 集合 | 时间字段 | 默认 | 清理边界 |
| --- | --- | ---: | ---: | --- |
| `AUTHENTICATION_RECORD_RETENTION_DAYS` | `authentication_records` | `createdAt` | 90 | 默认仅保留最近三个月；认证记录同时写入 `expiresAt` 并由 TTL 索引清理 |
| `AUDIT_RECORD_RETENTION_DAYS` | `audit` | `createdAt` | 0 | 按用户操作时间清理 |
| `RUNTIME_EVENT_RETENTION_DAYS` | `events` | `createdAt` | 0 | 按运行事件时间清理 |
| `COMMAND_HISTORY_RETENTION_DAYS` | `commands` | `createdAt` | 0 | 只允许 `SUCCEEDED`、`FAILED`、`CANCELLED`、`UNKNOWN` |
| `OPERATION_HISTORY_RETENTION_DAYS` | `operations` | `completedAt` | 0 | 只允许 `SUCCEEDED`、`FAILED`、`CANCELLED` |
| 固定 TTL | `request_events` | `createdAt` | 30 | 由请求事件 TTL 索引治理；修改该期限需要索引迁移 |

## 查询和清理原则

`common/record_retention.py` 只生成带截止时间和终态条件的查询计划，每个计划建议最多 1000 条一批，并由维护作业在删除前记录操作者、集合、截止时间和删除数量。活动中的命令、操作、运行、租约、幂等映射以及正在下载的文件不得进入清理候选；清理任务应使用复合索引先定位候选，避免全表扫描。

清理应与恢复流程协调：命令的 `UNKNOWN` 结果、操作的失败/取消终态和认证变更历史是排障证据，默认保留；如果业务需要缩短期限，应先导出审计摘要并在低峰分批执行。MongoDB TTL 适合无业务依赖的请求排障事件，不适合活动状态或需要事务判断的集合。

## 增长集合 Explain 验证

`scripts/benchmark_db_growth.py` 在随机数据库中生成认证历史、审计、运行事件和请求
事件样本，对首屏和深分页执行与正式接口一致的过滤、排序和分页，并输出 Mongo
`executionStats` 中的 `plan`、扫描键数、扫描文档数和耗时。脚本结束时删除随机库；
`--compare-indexes` 只在该临时库创建候选复合索引，绝不会修改线上索引。

```sh
python scripts/benchmark_db_growth.py --rows 20000 --compare-indexes \
  --output output/db-growth-$(date +%Y%m%d%H%M%S).json
```

报告出现 `COLLSCAN`，或 `totalDocsExamined` 明显大于 `nReturned`，应结合线上数据
评审过滤字段、排序字段的复合索引；`IXSCAN` 只证明该次样本选中了索引，不能代替
生产数据规模、写入压力和索引占用验收。MongoMock 没有真实查询优化器，单元测试仅
验证报告解析和分页契约，不能作为索引命中证据。认证历史优先使用资源 ID、时间和
ID 的游标分页，避免无限增长集合使用大 `skip`；总数统计应按需关闭或异步化。
