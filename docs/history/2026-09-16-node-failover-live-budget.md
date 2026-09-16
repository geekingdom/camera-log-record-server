# 节点接管、实时预算与配置模块验证

## 范围

本次实现异常节点任务的安全自动重新分配、实时日志最新内容预算、管理员配置预算以及后台横向模块导航。旧节点日志不复制、不删除；原始采集写入与浏览器缓冲独立。

## 已完成证据

- 后端全量：`pytest -q`，1512项通过；随后自围栏失败重试及Tick顺序修复的最终相关86项通过。依赖弃用警告保留，不影响结果。
- 前端：`npm test`，32个文件166项通过；`npm run build`通过，无大于500kB的构建块告警。
- `ruff check backend tests scripts`及`git diff --check`通过。
- `browser_resource_monitor_settings.mjs`在1440/390宽度验证四模块、草稿保留、预算保存和节点编辑，结果`passed=true`；截图已人工查看。
- `browser_live_terminal_workbench.mjs`验证1440/390/320终端布局、查找和命令历史，结果`passed=true`。
- `browser_live_ranges.mjs`验证1MiB内容预算下保留最新200行、淘汰最旧50行，并精确补读字节范围`[0,383400]`；覆盖先暂停、延迟返回较小配置后仍保持暂停并裁剪显示快照，控制台错误0。手机缺口弹窗截图已人工查看。
- `LiveLogBuffer`合成用例覆盖1200行/秒、连续20秒的输入，不按速率省略；还覆盖预算收缩、UTF-8、分包合并、传输去重及立即释放淘汰引用。这是状态机合成测试，非真实持续网络吞吐验收。
- `verify_isolated_browser_smoke.py`调用真实API、Worker、WebSocket和小时下载，结果`passed=true`；随机数据库`isolated_browser_smoke_77440e8b89c5445ca856f1861d4129a2`及临时日志目录均由验证器清理。
- 本机原有14个任务均为`STOPPED/STOPPED`后，受控更新API/Worker；两端`/health`返回`ok`，正式`GET /api/v1/display-settings`返回200和`{"liveLogBufferMiB":10}`。未启动或重启真实设备任务。

## 审查与纠正

故障转移审查要求围栏请求预写退避、核对完整精确关闭回执、限制收据消费批量并避免失败项饿死后续任务。数据库失联后各运行独立进入`releases`关闭流程，单个阻塞不阻止其他会话开始关闭；没有成功关闭证明仍不能接管。

前端审查发现淘汰数组仍可能持有旧正文、分包区间重复计入预算、显示快照重复复制以及暂停时下调预算不释放旧快照，均已修复并回归。预算按UTF-16正文和索引估算，不等同浏览器进程RSS；暂停时接收和显示快照会有各自的有界存储。

第一轮后端测试在子代理修改中执行，1507通过、2失败，分别为测试桩不接受位置参数和回执测试实例ID不一致；修正后最终全量1512通过。旧`browser_log_races.mjs`独立执行因缺少登录`/auth/me`模拟失败，未把它计入通过证据；正式隔离浏览器和本次终端/补读脚本已通过。

双Worker验证器默认监听回环80端口，在macOS普通用户下被拒绝；应使用`--isapi-test-port`指定高端口，不能把监听失败视为接管逻辑失败。

早期双Worker试跑曾在A归档READY断言处超时，未保留足够运行态以确认唯一根因，不能推定为60秒维护周期。源码表明正常`runtime.stop()`会等待最终归档发布；随后子代理与主代理各一次完整验证通过。数据库失联时`stop()`可能在物理关闭后因目录发布失败而抛错，新增专属`self_fence_pending`重试；Tick必须先移除已完成的release，再启动专属重试，最后才进入普通收尾，完整Tick回归已覆盖该顺序。本机Worker已在确认没有运行意图任务后再次更新至最后修复。

## 真实双Worker接管

主代理运行`.venv/bin/python scripts/verify_node_auto_failover.py --isapi-test-port 18089`返回0：

```json
{
  "passed": true,
  "oldRunId": "42efed7b88544ea3b880e644f2bccd85",
  "newRunId": "7a33f7ab85904c56a6eefc10faac0e85",
  "activeConnections": 0,
  "peakActiveConnections": 1,
  "connections": 2,
  "temporaryDatabaseDropped": true,
  "temporaryLogRootsDropped": 3,
  "failed": false
}
```

A、B各一份READY小时归档，分别位于`failover-a/`与`failover-b/`根目录；旧运行结束、旧端点锁释放，两次初始化已被源端收到。该脚本使用真实本机Mongo副本集、两个Worker进程、API进程和回环SSH源；关闭后台调度后确定性调用正式围栏、关闭收据消费和领取函数，证明组件间接管链路，不代表自然后台调度时序或物理网络分区已验收。新增流程已接入`cross-worker-read` CI，当前远端结果应按提交查询，不能以本机结果替代CI结论。

## 边界

完全失联且没有关闭证明或可信外部隔离证明时仍等待隔离。当前自动路径针对节点心跳/数据库失联，不对每个CPU、磁盘或写延迟告警强行搬迁。新节点创建新运行，初始化重新执行、定时预算按新运行重置；旧日志保持原节点目录，旧节点不可达时历史片段仍可能不可读。

本机短时测试不能替代物理多机、网络分区、断电、500路24小时混合负载及生产容量验收。
