# 输出恢复跨 Worker 租约验证

验证时间：2026-10-16（上海时区）

以本机已配置的真实 MongoDB 副本集创建随机临时数据库
`camera_output_recovery_verify_c62386d549d3402bafafd69fde75d863`，初始化资源集合后，对同一资源和
`SSH_SLAVE_1` 同时发起八次租约领取。结果为 `[true, false, false, false, false, false, false, false]`，
即恰好一个 Worker 获得租约；同一资源的 `SSH_SLAVE_2` 可独立领取。

对资源文档租约更新执行 `findAndModify` Explain，执行阶段为 `UPDATE -> FETCH -> IXSCAN`，未出现
`COLLSCAN`。查询以资源 `id` 为锚点，命中 `resources.id` 唯一索引，租约字段仅在该单一资源文档内
原子更新。

验证脚本在 `finally` 中调用 `drop_database`，上述随机临时数据库已经清理。该验证只写入临时数据库，
不涉及设备正文、下载副本或服务端任务日志。
