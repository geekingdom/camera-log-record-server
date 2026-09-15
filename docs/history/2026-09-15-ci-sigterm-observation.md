# cross-worker-read的SIGTERM状态观察竞态

## 失败证据与原因

CI34956216992（`f1f9561`）只有cross-worker-read失败，其余七项成功。失败步骤为`verify_worker_sigterm_resume.py`，Worker正常退出后等待任务`STOPPED + RUNNING + nodeId=None`满45秒超时；SSH日志显示18:10:08连接关闭，Worker正常结束，没有关闭异常。

`schedule_once`会将无候选节点的可恢复STOPPED任务更新为PENDING。Worker退出前已撤销准入，因此调度器可能在脚本轮询前完成该转换；原验证器要求持续观察瞬时STOPPED，存在时序误判。不是通过延长等待可解决的问题。

新增测试通过正式`schedule_once`将释放后的任务转换成PENDING，再调用脚本原判定，先复现失败。修复只改变验证器：允许STOPPED或PENDING，但仍要求无节点归属及RUNNING意图，并额外检查旧run已结束、runId未变、旧端点锁不存在。原有SSH连接数/名额归零、新Worker新运行/会话、初始化命令及源摘要检查保留。

## 验证

- 新增状态竞态及错误/隔离/未关闭/用户停止排除测试，相关39项通过，Ruff及差异检查通过。
- 本机真实Mongo、回环SSH、独立API/Worker，SIGTERM替换后恢复通过；两会话各12行摘要与源一致，临时库和日志全部清理。
- 第二轮在Worker退出后延迟观察2秒，实际读到`releaseObservedStatus=PENDING`仍正确完成恢复；连接/名额、旧运行结束/锁释放及两段摘要全部通过，随机库和临时日志再次删除。
- 本轮不修改生产调度和Worker收尾逻辑，不操作实体设备。
- 修复提交`ae9c671`的CI34958663525，cross-worker-read作业104346735930已在GitHub Ubuntu runner成功完成，包括原失败SIGTERM步骤。记录时其余部分作业仍在运行，不宣称全流水线成功。

先前总表中的GitHub计费阻塞是当时的检查结果；最新查询显示`d051785`及`d78874c`已有成功运行，不能继续以旧计费问题解释本次真实测试失败。
