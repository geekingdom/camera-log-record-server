# 2026-09-18 CI索引NUL兼容与资源创建状态修复

失败提交c4e2df5，CI35298177693。最终frontend、commit-messages、nfs-smoke、native-smoke、container-smoke、component-smoke成功，backend与cross-worker-read失败。

## 全文候选索引

backend为1失败1598通过，失败项是NUL后needle应保留候选的回归。本机SQLite3.54.0能够索引后缀，CI实际候选mayMatch=false。此前本机通过只能证明本机版本行为，不能证明Linux兼容；这是真实潜在漏检，不能跳过测试。

只在写入FTS前将NUL转为空格，原始日志、成员首尾和最终字节匹配不改变。含NUL查询仍走原文回退。schema从1升2，使已建立但可能不完整的旧索引失效，Worker既有健康探针会驱动重建。

新增确定性截断词法器测试覆盖短文本、32KiB索引块及256KiB解码块边界，兼验原文不变；新增schema1被拒并可重建测试。四项改前失败，改后通过。相关索引/规划/节点接口/维护/归档32项通过。

## 资源认证状态

cross-worker-read的真实ISAPI返回200、资源创建201，紧随的新建SSH任务409提示当前未认证成功。创建资源代码只写authenticatedAt而未初始化healthStatus，新增的网络任务准入校验因此拒绝，直到后续周期认证才可能恢复。修复需让成功认证与ONLINE状态一起进入创建文档，不能降低新建任务认证要求。

修复后创建成功文档同步写入ONLINE、healthCheckedAt、healthFailureCount=0和nextHealthCheckAt。正式资源创建后立即SSH任务201回归通过；`verify_cross_worker_ssh_resume.py --isapi-test-port 62137`真实Mongo/回环SSH双Worker链路通过，包括暂停释放、同run跨节点恢复、ISAPI复验、命令预算及停止；随机数据库和临时日志目录均删除，未访问真实设备。

本机全量1603项通过，末次资源/任务/索引维护另行回归；Ruff与差异检查通过。新增真实schema1 sidecar通过维护入口重建schema2测试，确认节点游标推进。前端无修改，不重复布局验证。新提交CI需按对应SHA核验，不能用本机成功代替Linux证据。
