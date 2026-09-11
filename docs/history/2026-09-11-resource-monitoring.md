# 资源监控实测与集成记录

本记录对应 `abe6d38` 之后的资源级监控开发工作区，不能代替最终提交的全量验证。

## 35设备CPU与内存采样

- 资源：`a92740bb7346469081a86daee24454ef`；任务：`24ba3743059243b787452da887aa00d8`。
- 通过正式API创建、暂停、恢复和停止。设备为默认ASH，复用采集SSH，没有额外探测连接，没有reboot或kill。
- 初始化依次发送outputClose、outputOpen、setDebug、prtHardInfo；每条延迟0.3秒。设备响应仍经唯一接收器写入日志。
- 2026-09-11 08:57:19.629 UTC首轮成功：MemAvailable 328424 KB、Slab 39468 KB、CPU idle 43.7%、Dsp_Main PID2127 VmRSS 94972 KB、Davinci PID2401 VmRSS 148608 KB、smart_bll PID3477 VmRSS 7832 KB、smart_ipp PID3515 VmRSS 80692 KB。
- 09:03:16.362 UTC提交pause，后续状态PAUSED/PAUSED；Worker TCP FD不含设备连接。09:05:11.118 UTC恢复前仍为6个样本，最新仍为09:02:22.138 UTC，暂停窗口超过一分钟没有新增采样。
- 恢复后run保持`9ac1702c555c4ca4a32371e2efd96b8e`，session从`b13a0ffc-d134-42b2-abd4-6d504e343553`变为`5bfcb3f2-e439-4987-bf7d-82e0562f44fc`。
- 09:06:16.052 UTC再次取得全部7指标，CPU idle 46.1%、MemAvailable 328864 KB。随后正式stop，确认STOPPED/STOPPED。
- 本轮说明默认规则可对该设备采样及暂停恢复，不证明所有型号、Telnet设备或多节点全天采样能力。后续失败隔离改动以自动化测试和最终重载为准。

## 清理与后续验证

停止后核对3条文件目录记录READY，分属两个小时包，原始字节合计2544072；该任务无活动日志作业。后续NFS实验完成后已正式软删除资源，确认5个实验任务全部STOPPED/STOPPED、nodeId空、SSH claims空，最终卸载状态UNMOUNTED。受限删除本次原生任务目录、8条READY文件目录记录，以及本次专用Docker容器和卷；未清理其它资源日志。

原生两小时包解压校验只有日志成员，压缩大小分别116179与154171字节；摘要分别为`c9b6b563173bec4be234129325f521396ae65abce9a89e58b8a0f39458702829`和`c47606c9f1906ab0e7ba427c0812a36748d7a7d1645bba589909c175f65667b0`。

## NFS资源生命周期与节点分配

在独立Docker NFS及Worker节点`resource-monitor-nfs-test`完成实测。节点配置capacity=2、isGeneralNode=false、resourceNetworks=[10.41.203.35]，原有通用local-dev同时在线。两路任务均自动优先分配到专用节点。

首轮发现设备BusyBox不接受以NFS来源作为umount参数，报No such file or directory。已修复为按/proc/mounts精确匹配来源，取实际挂载点执行umount -l，并再次确认该来源已不在挂载表。修改前真实sh模拟回归失败，修改后通过。

复验任务为`d95aaf3df6e64d6ab75cb449ea572139`与`8e6a60fd18904c6cbee41300c1df0b8e`：二者COLLECTING，前者首次持有MOUNTED；pause前者后后者接管MOUNTED，前者UNMOUNT_SKIPPED；pause后者得到PAUSED/UNMOUNTED；resume后者再次MOUNTED，最后stop并确认UNMOUNTED。全程不新开探测SSH、不发送kill/reboot。

Docker实验共4小时包、5日志成员，解压字节3593188，压缩字节362425。专用节点已正式软删除；容器`camera-resource-monitor-worker`、`camera-resource-monitor-nfs-server`和卷`camera-resource-monitor-nfs-test`、`camera-resource-monitor-runtime-test`已停止删除。

本次证明单物理设备、同Worker两SSH会话交接和最后卸载。跨物理Worker、Telnet设备协议、认证失败瞬间/网络断开等仍以模拟回归为证据，不宣称实体多机验证已完成。

## 默认规则、失败隔离与趋势

后台缺省配置直接包含参考脚本的3项系统指标和4类进程规则。首指标超时、单PID失败、规则预算耗尽分别隔离，成功值保留，异常按scope/code记录一次，下一轮继续。未启用或身份变化时禁止写入样本。

历史查询真实Mongo小样本Explain：2个小时桶，扫描2键/2文档、返回2桶，无SORT；只证明索引路径，不证明90天或500资源规模容量。

前端单图默认内存、可切CPU；KB存储值按可见量级统一换算显示单位。纵轴按选中曲线和时间窗口自动收紧，不强制零点，窄范围保留足够小数。隐藏曲线和缩放不会重置选择。1440/390浏览器验证默认/切换、迟到请求关闭重开、失败刷新保留、自定义时间实际请求、CSV和无横向溢出；截图检查通过。资源任务列位于设备认证与创建用户之间。

最终本机验证：后端1290项、前端136项，生产构建、Ruff及git diff --check通过；17项生产构建浏览器脚本通过。图表懒加载chunk约561KB（gzip约191KB），构建有体积提示但未失败。API/Worker在确认无活动归属后受控重载。
