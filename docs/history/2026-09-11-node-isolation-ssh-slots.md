# 节点隔离确认的SSH名额释放

核对起点b6c0abd。本轮检查跨节点接管路径，发现单任务恢复调用release_task_slots，而confirm_node_isolation遗漏该调用；管理员确认整节点隔离后旧任务名额可能遗留，最终占满设备五连接额度。

## 修复

在节点心跳检查和管理员隔离确认的原事务内，为BLOCKED SSH任务调用release_task_slots。只匹配taskId/runId/generation；停止、暂停、继续运行均释放已经证实隔离的旧连接名额，保留其他任务及后继代次。其他任务状态、锁、预算和控制意图沿用现有实现。审计或事务失败时不得单独提交名额释放。

这是已确认基础设施隔离后的数据库收尾修复，不能把过期心跳当作实际隔离证明，也不执行远程机器断电或网络隔离。

## 证据

- 新增RUNNING/PAUSED/STOPPED三个回归在修复前全部失败，旧claim未被删除；修复后通过。
- 隔离、任务恢复、关闭收据、SSH名额及事件接口定向40 passed；独立只读审核无阻塞项，隔离/名额15项通过。Ruff与diff检查通过。本轮未重复全量，不以历史1144项代替本次证据。
- verify_isolation_transaction.py在真实Mongo随机库运行：passed、transactionRollbackVerified、heartbeatConflictVerified、sshSlotsVerified均为true。
- 三个事务场景使用不同测试IP，任务明确为SSH。成功时三个旧claim释放，保留无关任务和后继代次两条；审计注入异常回滚全部集合，包括名额；外部新心跳引起事务重试后拒绝隔离，名额未变。
- 随机库在finally清理。本轮未操作设备、未新建采集任务、未生成设备日志。本机API重新加载修复。

## 后续

真实Linux多机的进程终止、网络隔离和设备socket消失仍需目标部署验收；本记录仅证明隔离确认后的数据库事务边界。前端只读审查另发现任务页重复请求和App.vue超过建议行数，已更新总表，不混入本次后端修复。
