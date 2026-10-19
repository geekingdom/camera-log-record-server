# 2026-10-19 从机运行时身份巡检

## 背景

从机 SSH 任务在首次建立时已经以 `ifconfig` 核对目标内网地址，但运行期间嵌套
shell 意外退回主机时，后续日志仍可能来自主机。该情况不能通过额外 SSH 连接探测，
否则会违反每会话单接收器和命令发送队列约束。

## 实现

- `Collector` 对 `SLAVE_1`、`SLAVE_2`、`SLAVE_3` 的 SSH 会话在初始化完成后启动
  `slave_identity.py` 中的 60 秒巡检协程，Collector 只保留会话生命周期的启动和取消。
- 巡检经已有 `CommandDispatcher.capture_command` 串行发送带随机起始标记的 `ifconfig`，
  响应仍只由现有 reader 接收、保存和旁路捕获；不建立新连接，不占用用户定时命令预算。
- 巡检只解析随机起始标记之后的本次 `ifconfig` 输出，起始标记前已经抵达 reader 的设备
  日志、命令回显或旧 IP 不能参与本次身份判定。标记边界兼容 SSH PTY 的 LF、CRLF 和
  跨网络分包。
- 首次引导和运行时巡检共用地址验证：响应必须包含目标从机地址，且不得包含任务配置
  的外层主机地址。无法完成捕获、通道不再安全或地址不匹配均关闭当前会话。
- 关闭交给既有 `SessionRuntime` 从机固定 10 秒重连路径；下一会话重新执行完整主机借用、
  从机登录、ASH 和目标地址验证。
- 收尾路径取消巡检协程；发现 `ShellBootstrap.identity_lost` 时也拒绝继续发送恢复或输出
  控制命令。

## 验证

新增回归把巡检间隔缩短为 10 毫秒，模拟捕获到仅含主机 `10.41.203.35` 的 `ifconfig`
输出，验证巡检命令经过捕获队列发送且会话随后关闭。未实现常量时该测试以
`AttributeError` 失败；实现后通过。另覆盖起始标记前含主机 IP、标记后为目标从机 IP
时持续采集；主机任务不创建巡检协程；停止取消未到期的从机巡检；巡检等待已开始的人工
命令并在其后串行写入；首次巡检关闭会话后 `SessionRuntime` 确实建立第二个从机会话。

`tests/test_collector.py`、`tests/test_slave_ssh.py`、`tests/test_slave_debug_commands.py`、
`tests/test_slave_recovery.py`、`tests/test_protocol_bootstrap.py` 与 `tests/test_runtime.py`
相关专项共 129 项通过，相关 Ruff 检查通过。真实 AsyncSSH 空闲重连专项也通过（1 项、
20.93 秒）；其保留同一运行预算、关闭后再建连接和 10 秒空闲窗口，确认巡检的收尾协程
不改变既有重连行为。
