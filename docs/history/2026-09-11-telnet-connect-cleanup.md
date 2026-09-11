# Telnet协商取消与超时清理

起点提交`5a0ff89`。本轮继续采集生命周期目标，未修改前端、数据库结构或日志格式。

## 根因与修复

当前telnetlib3的open_connection先await create_connection建立TCP，再await protocol._waiter_connected等待协商，最后才返回reader/writer。原工程在返回后才进入登录清理try；协商中取消或超时，已接通TCP没有交给工程清理。

connections.py增加可观测工厂，继续创建原LogTelnetClient，在connection_made后捕获writer；整个open_connection及登录均位于异常清理范围。默认建连阈值仍30秒。取消时独立有界关闭并shield；二次取消立即abort，后台关闭异常自行记录而不形成未读取Task异常。原始取消、超时和登录错误继续向上传播。

## 验证证据

- 修改前相关基线：连接/传输/SSH空闲/存储29项，时间戳/回拨/Telnet保活10项通过。
- 后端全量1161项通过（108.27秒）；连接影响范围定向20项通过。最终仅补强测试时序和清理后，新测试3项再次通过；全仓Ruff和diff检查通过。
- 新测试用真实本地TCP服务和telnetlib3客户端，保持协商未完成。取消场景等待connection_made后50ms并确认调用仍未完成，避免只测TCP建立期间的取消。取消及缩短超时后，服务端均在1秒内观察到EOF。
- 独立Python进程将git show 5a0ff89的旧连接模块载入内存，运行相同取消测试：peer_eof等待1秒抛TimeoutError（1 failed）；当前源码同测试通过。没有覆盖或回退工作区文件。
- 最初过早取消的测试旧版也通过，不能作为原泄漏证据。加明确协商等待后，旧版泄漏还使测试server.wait_closed等待；该诊断进程已中断退出。测试finally现主动关闭自建服务端连接并等待handler，失败对照可正常退出，不留下监听或设备连接。
- 二次取消使用挂起writer验证abort，并用事件循环异常处理器检查没有未处理异常。测试超时被缩短，不代表真实网络故障一定在相同毫秒数收敛。

## 日志合同核查

行前缀、10MiB严格分卷、part序号、安全任务名/IP/上海小时范围归档名、仅.log成员、成功发布后删除原.log均已有实现和回归，独立审核未发现本轮阻塞缺口。索引与metadata仍留在Worker旁路供定位和校验，不进入下载包，不是需要删除的原始日志分卷。

## 本机状态与边界

重载前只读确认期望RUNNING任务0、QUEUED/RUNNING日志作业0；通过既有launchctl服务重新加载Worker。8001/health返回ok，Mongo节点local-dev心跳年龄0.915秒。首次检查误用了heartbeatAt字段，随后对照worker.py核实真实字段为heartbeat后完成检查，未据错误字段判定节点失联。

未连接34/35或串口硬件，无新增业务采集任务或下载产物。回环测试监听与客户端均有收尾；pytest存储回归使用框架临时目录，不清空业务日志目录。本轮不证明实体Telnet硬件、物理跨节点隔离或500路全天容量。
