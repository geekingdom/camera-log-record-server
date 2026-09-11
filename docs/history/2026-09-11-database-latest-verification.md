# 数据库最新需求复核

日期：2026-09-11；代码起点：4684984。最新用户业务要求是按实际查询建立索引，认证历史改为三个月内保留并聚合连续成功。交接摘要的Worker模块任务不是本次业务指针；保留该未提交修改，不混入本次数据库结论。

## 当前实现和验证

- `authentication_records.py`按同资源、同UTC日、同身份的连续周期成功聚合，保存首次/最近时间和次数；失败、人工操作和身份变更保留独立边界，事务头防止并发越过边界。
- 业务库实读索引数：authentication_records 8、audit 6、events 5、request_events 10，含主键和兼容旧索引；认证expiresAt索引expireAfterSeconds=0，请求记录TTL为2592000秒。
- `backfill_authentication_expiry.py`预览：retentionDays=90、eligible=0、updated=0。没有修改业务记录。
- 认证保留、记录策略、事件索引、认证接口和基准解析五个测试文件合计29项通过；仅有既有依赖弃用警告。
- `verify_event_index_migration.py`真实Mongo旧索引双初始化通过。审计/请求默认及筛选查询均返回50条、扫描50键和50文档，无COLLSCAN及阻塞SORT。
- `verify_authentication_history_concurrency.py`通过，headRevision=33；失败记录保留，成功区间计数未跨越失败边界。
- `benchmark_db_growth.py --rows 20000 --compare-indexes`通过；认证首屏返回20条扫描20键和文档，审计/请求返回50条扫描50键和文档。

## 保留限制与后续

认证三个月采用90天，按日分段避免累计计数不断延长单条历史寿命；TTL异步物理清理，接口过滤过期记录。时间筛选返回相交整段，累计次数不是筛选子区间的精确次数。旧历史不推测补造聚合次数。

认证深分页20条仍扫描5020个键；运行事件按派生时间排序仍有SORT，样本扫描2500条。这两项需分别改前端游标及规范持久时间字段，不能仅叠加索引解决。生产写入成本、索引容量、复制延迟及全天负载未在本次验证。

随机验证库由脚本finally删除并确认；没有访问设备、生成采集日志或下载产物。本次只更新复核文档，不将已实现需求重新列为源码待办。
