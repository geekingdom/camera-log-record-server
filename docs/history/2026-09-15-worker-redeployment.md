# Worker 受控重部署恢复

## 根因与行为

旧 `Worker.close()` 在退出时无条件把活动任务的 `desiredState` 写成 `STOPPED`。Docker虽按`unless-stopped`重新启动Worker，调度器也不会再启动这些任务。修复位于`node/shutdown.py`及`node/worker.py`。

- 退出先将节点准入设为false，避免有效旧心跳期间继续领取；等待已经开始的运行收尾，再以每批最多16路处理剩余会话。
- 每个实例先关闭实际连接与日志，再按原owner重新读取最新用户意图。RUNNING保留运行意图，确认关闭后释放旧运行；新Worker自动建立新运行并重放初始化。PAUSED保留暂停及预算，STOPPED保持停止。
- 关闭期间用户新提交的暂停不会被原先读取的RUNNING快照覆盖。运行归属丢失、BLOCKED或状态读取失败只隔离旧连接，不盲目释放锁。
- 节点准入撤销失败时不把活动任务释放给调度器；已知关闭错误沿用BLOCKED故障路径，不再次执行未确认关闭。
- 完整Docker、独立Worker及旧Worker部署文件均增加`stop_grace_period: 120s`；原生systemd已有`TimeoutStopSec=120`。并发关闭和更长窗口降低正常重部署被默认10秒SIGKILL截断的可能，仍不能保证永久卡盘在120秒内完成。

## 实际验证

`scripts/verify_worker_sigterm_resume.py --isapi-test-port 18080`使用真实API、真实Mongo副本集、回环SSH/ISAPI源和随机日志根。经正式API创建已认证资源和SSH任务，向旧Worker进程发送SIGTERM，观察旧进程退出、源端连接关闭及Mongo名额0；以相同nodeId和日志根启动替换Worker。

替换Worker自动进入COLLECTING，runId与sessionId均更新，两个会话均实收四条初始化命令。两个会话各12行，正式内容API按sessionId读取并完整比对，SHA-256分别为`75996a73227f5282bbf596768a8e494d7f73c1407eddc495b76d767e37cd55d2`与`e700b6924a2c0ba40bc6e17047f2afb41a7fe1f6864270eae8aaca7ad954df02`。最后正式stop后任务STOPPED、nodeId为空、源端连接和名额0；随机数据库与临时日志根清理完成。

本机80端口无权限，因此仅隔离API启动器适配高端口。Linux CI加入默认80端口的相同真实进程验证，完整Docker部署作业另验构建、健康和重跑。该脚本不是物理宿主机重启或断电测试。

## 升级边界

正常重部署恢复会建立新运行，定时预算重新开始；这与用户暂停/恢复保留原运行不同。旧版本已经写成STOPPED的任务不能可靠地区分是历史部署停止还是用户主动停止，不自动批量开启。首次从旧Worker升级时，退出仍可能执行旧镜像的关闭代码，需在升级后核对这些任务并手动启动一次；后续运行新版本的受控重新部署按上述规则恢复。

永久磁盘错误、SIGKILL、无法确认旧连接已释放或数据库不可用时，保留风险和隔离状态，不能为自动恢复直接清除锁。公司服务器拉取并重新构建Worker后才能应用修复；仅更新前端没有效果。
