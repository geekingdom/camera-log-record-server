# 本机服务更新与来源 IP 边界复验

代码基线：`7b4964e809f707ecbea3e1adbcda687b6216e474`。本记录仅证明 2026-09-09 本机观察结果，不代表 500 路全天容量验收。

## 平台来源 IP

重新阅读 `access_policy/policy.py`：匹配输入为 ASGI `request.client.host`，不是资源或采集目标字段。最终权限为账号权限与来源规则权限的交集。

实际执行 `pytest -q tests/test_ip_policy.py tests/test_ip_policy_integration.py tests/test_access_log_boundary.py`：10 passed。覆盖非白名单目标设备资源创建、非白名单串口目标任务创建和启动、功能权限隔离、来源拒绝审计。无须把设备、SSH/Telnet 目标或串口服务器 IP 加入平台访问白名单。额外代理拓扑仍需正确配置可信来源链。

## 本机服务与实体任务

恢复时收取既有执行会话 3376 的结果，没有再次提交命令：两路任务恢复至 COLLECTING/RUNNING，generation 为 28，保留原 runId。34 的会话为 `77d92710-7e94-4abf-bf9f-cf848dec1682`，35 为 `0fc4c537-e1bb-45cd-a873-8ee7f2b2816e`。

本轮重新通过 ps/lsof 核实 API PID 25864、Worker PID 25871；旧 PID 39773/83027 已不存在。新 Worker 分别持有一条到 34:22 和 35:22 的 ESTABLISHED 连接。

| 设备 | 任务 ID | 原运行 ID | 手动命令 ID / 结果 |
| --- | --- | --- | --- |
| 10.41.203.34 | 3a80edb2bc3d49e7836ef0f594e07a50 | fef2f6f61e0d4f858e03dc8fdea367ee | b79b8556faea4dfba2e98f1914484b2a / SENT |
| 10.41.203.35 | 02e9cb46b133489ab124bf55fac684e1 | 68d7e15b5b30429faf7dcb1884b6e92b | 7be1326eabd242c99ad9c0220bd54eb9 / SENT |

命令为每设备一次 `prtHardInfo`。数据库查询确认两条对应审计，两个任务 commandClaimVersion 均为 2。SENT 只证明发送完成，不等同设备业务执行成功。

三秒观察窗口内，OPEN 文件的磁盘大小和目录 bytes 同步增长：34 从 521263 至 521490 字节，35 从 1534595 至 1547328 字节。首次探针误读 rawPath 得到 0，随后按实际 OPEN 目录的 path 字段修正并直接 stat 文件；不将错误探针解释为日志丢失。

未发送 debug，未新增采集任务或下载副本，未删除真实设备日志。服务进程继续运行。此前历史文档中的“未重启”描述仅代表其记录当时，本轮已完成服务更新。

仍待完成：实时缺失范围补读、一般业务变更与审计一致性、控制操作故障一致性、跨节点物理隔离及集群全天容量验收。
