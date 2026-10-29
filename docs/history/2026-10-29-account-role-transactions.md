# 账户角色与手工命令真实事务验证

`scripts/verify_account_roles.py` 使用真实 MongoDB 副本集、随机数据库和临时日志目录运行正式 ASGI 路由。脚本退出时删除随机库和临时目录，不连接真实设备、不启动采集、不创建日志制品；仅插入并清理模拟任务文档，也不停止外部启动的 MongoDB 实例。

2026-10-29 本机以 `mongodb://127.0.0.1:27329/?replicaSet=verify1029` 运行通过，输出：

```json
{"passed": true, "managerFence": true, "createAuditRollback": true, "adminProtection": true, "ordinaryManualCommand": true}
```

验证覆盖：

- 暂停管理员请求至 `claim_account_manager`，超级管理员将其降为普通用户后恢复；迟到更新返回 403，目标账户与 `edit_user` 审计均不变。
- 注入创建账户审计失败；接口返回 503，用户文档回滚。
- 管理员不能编辑、重置或删除其他管理员，也不能创建、查看或撤销其服务令牌。
- 普通用户凭固定 `commands:send` 向其他创建人的采集任务提交手工命令；重复同一幂等键仅保留一条命令、一条审计和一条幂等映射。

该验证证明副本集事务与上述竞争边界，不构成设备连接、手工命令实际发送或容量验收。
