# 任务控制审计事务

本记录覆盖任务 start、stop、pause、resume 的控制意图与审计一致性，以及创建任务时自动启动的整体事务。它不代表 Worker 的物理连接收尾已经事务化。

## 控制语义

`tasks/control.py` 的 `request_control` 以固定 operation ID 在 MongoDB 快照、majority+journal 事务中处理控制请求。每次事务回调先递增任务 `controlClaimVersion`，对关联资源按稳定顺序递增 `controlClaimVersion`，再处理已有操作、相反 PENDING 操作、新操作、任务期望状态和审计。资源上的写入是与软删除竞争的冲突屏障，不改变 Worker 的 `generation` 或运行所有权。

任务保存 `controlOperationId`，指向当前已接受的控制操作。若同一目标的指针操作仍为 PENDING，重复请求直接复用它且不写第二条成功审计；若指针操作为 SUCCEEDED，只有任务已到达实际目标才复用。`resume` 使用 RUNNING 目标并要求任务已完成 SSH 暂停，因此重复 resume 仅复用已经接受的恢复操作，不会创建第二个运行或重置预算。

操作记录包含 `id`、`taskId`、`desiredState`、`actor`、`status` 与 `createdAt`；已成功操作另含 `completedAt`。控制审计动作是 `control:RUNNING`、`control:STOPPED`、`control:PAUSED`。操作状态为 PENDING 仅表示意图已提交，连接打开、关闭和运行状态推进仍由 Worker 完成。

无节点的 STOPPED/PENDING SSH 任务暂停会检查该任务没有运行锁，成功后清除陈旧 `runId` 与 `sessionId`。无节点的 PAUSED 任务停止会在同一事务内以 `taskId + runId` 删除匹配 lock、给相同 run 写入 `endedAt`，并保留该 run 的 budgets。恢复仍要求 SSH、PAUSED、`desiredState=PAUSED` 及 `nodeId=None`，不满足返回 409。

## 资源删除与失败

资源删除和控制事务都写关联资源文档。删除先提交时，控制事务重试后资源谓词不匹配，start/pause/resume 返回 409；控制先提交时，删除的后续扫尾将任务写为 STOPPED、`resourceDeleted=true`，并取消不适用的 PENDING 操作。因此已删除资源不会留下新的 RUNNING 意图。

审计写入失败会回滚任务、资源 control claim、operation、新旧 PENDING 状态、endpoint lock、run 和 budget 的本次变更，并以 503 返回。通过 HTTP 生命周期注入 `CancelledError` 时，Starlette 中间件会给调用方 500；这不代表成功，真实数据库验证确认所有上述写入同样回滚。直接调用事务入口的取消会继续向上传播。提交 ACK 丢失时，控制实现只读取本次固定 operation；验证器还确认返回 operation 与任务 `controlOperationId` 一致，不能重做业务写入。

## 真实验证与清理

`scripts/verify_task_control_transactions.py` 使用当前 `Settings.mongo_uri`、随机临时数据库、临时 token/Fernet key、`admin_password=""`、`start_background=False` 和 `create_app` 正式生命周期。它经 `httpx.ASGITransport` 调用真实控制与资源删除路由，不启动 Worker，不访问设备，也不使用主数据库。

真实副本集验证覆盖审计错误和取消回滚、相反 PENDING 取消回滚、八个并发相同启动复用同一 operation/审计、相反意图的唯一 PENDING 指针、暂停后停止的 lock/run/budget 语义、资源删除先后次序与并发、以及提交 ACK 丢失确认。脚本通过后才输出成功 JSON；随机数据库、Mongo client 和 `TemporaryDirectory/logs` 均在 `finally` 中清理。全量测试、构建和 CI 结果由当前状态总表在实际执行后记录，本文件不预写。

## 未覆盖的外层边界

Worker 释放连接、推进 operation 到最终完成和处理网络结果属于物理运行时收尾，不在 API 事务内。设备 socket、命令发送和结果未知语义也不由本模块改变。任务编辑、登录/退出会话以及其它作业的业务审计仍需后续处理。

## 自动启动创建

独立审查发现旧创建流程先写任务，再提交控制，控制异常时会删除幂等映射并遗留任务，造成同键重试可能重复创建。最终 `tasks/creation.py` 在同一个事务内写任务、成功幂等映射、自动启动操作和两条审计，并与资源删除通过实际资源声明写冲突；准备阶段的加密和命令复制只执行一次，不嵌套控制事务。

`creationOperationId` 保存不可变的初始自动启动操作，`controlOperationId` 可随后续控制更新，因此同键重试创建不会错误返回后来的停止操作。无自动启动的创建不会附带后续控制操作。真实副本集验证已覆盖自动启动成功、同键重放及审计失败后任务、映射、操作和资源声明全部回滚。
