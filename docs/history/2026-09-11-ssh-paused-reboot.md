# SSH 暂停期间设备重启验收

## 范围与前置

从 `abe6d38` 的干净工作区开始。2026-09-11 使用用户授权的 10.41.203.35，SSH 22，设备默认 ASH；只发送一次 reboot，不发送 debug 或 kill。主代理核验当前监听 Worker PID 为 85561，无其他活动任务；通过正式 API 创建并认证临时海康网络资源。凭据从本地受限配置读取，不进入报告。

- 资源：`359ca2ead22f44e7b7528d28ea248815`。
- 任务：`fd20ab5b9bd64b7cb127e059f26ee278`。
- 初始化依次为 `outputClose`、`outputOpen`、`setDebug -m all -l 7 -d 111`、`prtHardInfo`，各延时 0.3 秒，无定时命令和 NFS 监控。
- 执行 `verify_ssh_paused_reboot.py --worker-pid 85561 --task-id fd20ab5b9bd64b7cb127e059f26ee278 --timeout 420 --reboot-confirm`，通过正式任务连接发送命令，没有额外 SSH 探测。

## 实测结果

脚本退出码 0，`passed=true`，收尾 `SUCCEEDED`。

| 阶段 | 证据 |
| --- | --- |
| 启动采集 | COLLECTING，runId=`437d024ef4854543985ef13802baec21`，一个 SSH FD、一个数据库连接名额 |
| 重启命令 | `a088a395585244ec81c15d6cd4df383a`，SENT，仅发送一次 |
| 暂停 | PAUSED，desiredState=PAUSED，保持原运行、nodeId为空；持续检查 FD 与名额均为零 |
| 暂停观察 | 65.195 秒；第 54.813 秒首次观察资源 OFFLINE；提交恢复前累计观察 OFFLINE 10.382 秒 |
| 离线恢复请求 | 正式 resume 返回202，WAITING_DEVICE，desiredState=RUNNING，原运行、无节点及连接名额 |
| 设备上线 | 资源 ONLINE，COLLECTING，保留原运行；会话由 `fc62f1ab-efdc-440d-ac77-5965a05c71c8` 变为 `bed05030-fcc5-4c68-b468-e2c29d613855`，一个 FD、一个名额 |
| 最终停止 | STOPPED、desiredState=STOPPED、nodeId为空；独立查库名额为零，lsof无设备连接 |

设备认证 OFFLINE 时间为 `2026-09-11T08:19:49.966Z`，恢复 ONLINE 时间为 `2026-09-11T08:21:01.230Z`。该结果证明暂停超过一分钟、资源离线缓存存在时显式恢复可进入等待并最终采集；认证为定时抽样，不能据此报告设备精确断电/启动时长或声称连续直接探测离线65秒。

## 产物清理

正式 API 软删除本次资源，保留任务与运行审计。确认无活动日志作业、两个文件目录记录均 READY 后，验证小时 tar.gz 只有两份 `.log`，解压总计 1,299,661 字节，压缩大小 103,542 字节。

归档 SHA-256：`a9f6e329d353aef0d69ea67ed3e206487843d51dc5f2ad475053250d830ded65`。

只删除本次明确任务 ID 对应的目录及两个文件目录记录，未清空公共日志根目录，也未删除其他设备产物。日志正文和凭据不入库。

## 验收工具修正

独立复核发现生命周期脚本允许零次循环仍通过、停止收尾未检查期望状态、暂停重启脚本缺少资源类型前置。补充正整数循环校验、STOPPED期望状态、未删除海康资源及端点一致性校验。显式任务ID和reboot确认仍是执行选择，不增加名称约束。三份相邻脚本回归35项通过；实机流程使用修正前脚本，本轮前置和收尾已由主代理独立核验，未为验证工具改动重复重启设备。
