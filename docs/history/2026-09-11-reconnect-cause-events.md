# 采集重连原因与模块拆分验证

核对起点为b835bba。本轮继续异常记录与模块性目标，未重复数据库认证聚合改造。子代理独立实现runtime事件和验证器，主代理整合展示/筛选、审核、全量测试和真实设备验收。

## 实现和边界

- runtime.py由525行降至494行，状态事件集中在runtime_events.py。
- READ_ERROR和IDLE_TIMEOUT先匹配任务运行归属并更新RECONNECTING，再保存原始事件类型、任务/运行/会话/节点及固定中文原因；不保存设备正文或原始异常文本。
- 退役、BLOCKED、失去归属或非当前Collector会话的状态回调不覆盖新会话；时钟回拨仍保留块序号和文件ID，不改采集状态。
- 统一事件写入最多等待1秒，包括原有CONNECTION_GAP；失败或超时写服务异常日志，取消仍传播。这仅隔离观测事件故障，不绕过任务状态、预算及归属更新的数据库安全要求。
- 页面摘要与Mongo派生筛选一致：两类原因均UNKNOWN/WARNING。验证器现在要求本次命令后旧session的IDLE_TIMEOUT事件存在，不再仅凭session变化判断原因。FD快照仍不能排除采样间隙内的短暂重叠。
- 复用events现有索引，不新增集合；数据库策略文档记录持续异常时的增长预算及未实施的分类清理边界。

## 验证

全量后端1144 passed，109.42秒；Ruff和diff检查通过。定向测试覆盖旧会话/退役/BLOCKED、事件写入抛错或卡住、时钟回拨兼容、默认10秒重连顺序、展示与筛选及验证器原因证据。真实Mongo随机库插入两类事件，经正式分页实现筛选UNKNOWN/WARNING返回2条且中文摘要正确，随机库已删除。

确认无活动任务及SSH名额后，用launchctl重新加载API/Worker；8000/8001健康200。35设备经正式资源认证创建专用资源7de364ee5b62456c9a75658762db02dd、任务42924aa2a6c14521bcec5c6cede837db。初始化为用户四条命令，每条0.3秒；不执行debug、reboot、kill或NFS操作。

执行verify_ssh_lifecycle.py，worker-pid=5630、cycles=0、verify-idle-reconnect启用，退出0且passed=true。本轮不重复暂停循环（上轮已验证）。发送outputClose后42次FD采样未超过1，出现同run的新session，名额和FD1；stop成功归零。

正式运行事件API按taskId/type=IDLE_TIMEOUT/outcome=UNKNOWN/level=WARNING返回200、total=1，中文摘要“采集日志空闲超时”。runId为23231492f6d34c24bac70359d69165c3，旧session为a953db26-9561-4431-9e6d-f161b831f0d0，事件时间2026-09-11T03:47:49.790Z。该记录直接来自本次真实Worker检测，不是插入业务库的模拟事件。

## 清理

专用任务停止且无名额、无活动日志作业后，经正式API软删除资源。1个小时压缩包仅含2个.log成员，解压长度643800和652708字节且可读至末尾。归档/索引/元数据共162440字节，按显式文件ID通过受限开发清理入口删除：removed=2、failures=0、目录登记和跟踪文件剩余0。保留软删除与事件审计，不保存设备正文至Git，不改变全局保留期限。
