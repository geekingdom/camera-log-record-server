# 计划重部署任务恢复

## 问题

单节点执行`deploy-all.sh`重新部署时，Worker退出与新Worker心跳恢复之间可能超过调度器的心跳阈值。调度器会把仍有旧节点归属的任务置为`BLOCKED/等待隔离`；新Worker原先只处理精确关闭收据，导致这些任务长期停留，需要逐个手动重启。

## 实现

- Worker关闭路径继续先撤销节点准入并关闭现有连接。
- 新Worker启动后，在首次心跳前检查同节点的计划关闭标记、未准入状态以及旧Worker写入的`shutdownCompletedAt`和token。
- 旧Worker在所有运行关闭和数据库收尾完成后才写入完成标记；新Worker只对带`plannedRestart`且满足该精确完成标记的任务结束旧run、删除旧端点锁、清理旧收据，置为`STOPPED + desiredState=RUNNING`，交回调度器。若进程被强杀或某个连接未收尾，完成标记不会写入，任务继续走隔离证明流程。
- 调度器因此优先选择其他在线节点；没有其他节点时等待原节点重新准入后再领取。
- 没有`shuttingDownAt`的普通失联任务不走此路径，仍要求原有隔离收据或明确确认。
- 任务列表将`restart`加入批量生命周期操作，按每个任务调用既有隔离恢复接口，逐项显示失败和跳过结果。
- Worker 启动仍先执行`recover_orphan_archives`，对带`.pending.json`的未完成小时压缩继续提交归档，对已发布归档补回目录记录；不会删除未完成的原始日志。

## 验证

`tests/test_worker_ownership.py`计划重启恢复、普通失联保护等29项通过；`frontend/src/features/tasks/taskActions.test.ts`生命周期10项通过；前端生产构建和差异检查通过。真实多节点重部署、跨主机断电和旧连接无法确认的场景仍需部署环境验收。
