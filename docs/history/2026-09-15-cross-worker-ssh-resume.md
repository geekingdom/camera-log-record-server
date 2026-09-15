# 同机双 Worker 真实 SSH 暂停恢复

## 范围

用户暂时没有可提供的目标 Linux 服务器，本轮补齐本机可验证链路。使用随机 MongoDB 数据库、一个真实 API 进程、两个独立 Worker 进程及独立日志根、回环 AsyncSSH 和 ISAPI 模拟设备。通过正式 API 创建资源/任务、修改节点准入、暂停、恢复、读取日志和停止，不直接修改任务状态模拟迁移。

脚本为 `scripts/verify_cross_worker_ssh_resume.py`，源端和隔离启动器分别位于 `cross_worker_ssh_resume_source.py`、`cross_worker_ssh_resume_api.py`。

## 本机命令与结果

```sh
.venv/bin/python scripts/verify_cross_worker_ssh_resume.py --isapi-test-port 18080
```

本机当前用户无法绑定80端口，且没有免密sudo。显式参数仅在隔离API子进程中改写认证URL端口，仍使用生产认证客户端、Basic凭据校验和实际HTTP响应；不修改生产配置。本地高端口结果不证明标准80端口绑定。Linux CI以相同Python解释器、sudo和默认80端口独立运行。

- A节点真实采集后，正式pause操作完成；SSH服务端观测连接关闭，Mongo连接名额为0。暂停窗口2.2秒无自动重连。
- 正式节点配置关闭A准入、开启B准入。resume触发认证请求；在ISAPI响应闸门关闭时确认WAITING_DEVICE、nodeId为空、原runId、RUNNING意图、PENDING操作和零连接/名额，再检查2.2秒没有重连。重复resume复用相同操作。
- 放行认证后B开始采集，沿用runId且生成新sessionId，资源认证完成时间晚于恢复请求。两个会话分别实收初始化：outputClose、outputOpen、setDebug -m all -l 7 -d 111、prtHardInfo。
- 定时probe间隔5秒、总预算2；A/B各实际收到一次，正式执行记录共两次SENT。暂停恢复没有重置预算。
- 暂停/停止前先冻结模拟源输出，等待明确可读水位；正式内容API续读实际文件，与源端全部正文逐行等值比较，不忽略其它会话或额外正文。A/B各26行、各一个文件，正文SHA-256分别为 `764d4d204eae83b129cc887ddede9f78c55c8acac199e82bdbfed028df6ec1e4`、`1619ee01c34d772eb4002605e8cf0f2cef5e57a1dc482827b5a225f4a9190f3e`。
- 最终STOPPED、nodeId为空、源端连接和Mongo名额为0。隔离进程退出，随机数据库与临时根（包含两个Worker日志目录）删除；失败诊断和句柄关闭错误不会跳过随机数据库清理。

## 实验修正与限制

首轮因80端口权限失败，后改用上述显式测试适配；早期验证器复用资源与任务创建的幂等键，收到409后改为各请求独立键。未因此修改生产认证或幂等行为。失败运行同样清理隔离数据，没有连接真实设备。

本轮证明合作暂停、认证后恢复到另一独立Worker的实际协议和日志链路。它不是旧Worker失联后的强制接管，不证明物理主机网络分区、远程隔离、SSH长期压力或500路全天吞吐。跨节点日志目录补读及停止单节点503隔离是另一个已完成验证，见[双Worker补读记录](2026-09-15-cross-worker-gap.md)。
