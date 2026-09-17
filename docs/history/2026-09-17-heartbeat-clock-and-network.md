# 接收心跳时钟与网络速率核验

## 最终合同

节点`heartbeat`由Mongo在更新时生成，Worker墙钟只写`workerReportedAt`供诊断。节点时钟正负8小时偏差不会影响在线或调度候选，仍保留15秒准入、30秒离线窗口；所有原有心跳查询复用同一字段。没有给任意业务租约放宽8小时，也没有修改真实日志的时间戳。

遥测用单调采样年龄换算平台采样时间；当前API返回校准`sampledAt`和原始`workerSampledAt`。停止采样的旧数据不会因继续上报心跳而刷新。新增字段只覆盖节点现有文档，不增加集合或持续历史记录，不需新增索引。

用户最终确认网络单位为Mb/s、Docker部署。原平台B/s以KiB/s或MiB/s展示，10Mb/s应等于1250000 B/s、1220.7KiB/s、约1.19MiB/s，不能直接比较数字10与几百。现网络上下载统一显示Mb/s，悬浮保留字节速率、实际网卡和采样窗口，采集输入仍为字节单位。修复lo前缀误排long0，未修改已有字节差分公式；无法据此确认公司现场差异的唯一原因。

## 验证证据

- 后端全量1518项通过；随后网络元数据及提示文案最后相关46项通过。
- 前端167项、生产构建、Ruff与差异检查通过。
- `browser_node_dashboard.mjs`1440/390浏览器通过，确认10.00Mb/s及悬浮1.2MiB/s、eth0、1.20秒；手机截图人工检查，无组件重叠。
- 真实`verify_node_heartbeat_clock.py`模拟Worker本机心跳与遥测墙钟均偏移正负8小时，Mongo生成心跳递增、校准采样有效、高CPU拒绝领取、正常CPU真实事务领取成功、停止上报31秒离线；最终JSON所有标志为true，随机数据库和临时日志目录已清理。
- `verify_isolated_browser_smoke.py`真实API/Worker/WebSocket/小时下载通过，临时数据库`isolated_browser_smoke_c264038f4b054d4fa038913b23c6dd1e`及日志目录已删除。
- 旧`browser_node_latency.mjs`的独立mock缺少`/auth/me`而失败，未作为通过证据；使用当前完整`browser_node_dashboard.mjs`完成看板验证。
- 收尾复核：本机API、Worker健康接口与5173前端均返回200；正式`/api/v1/nodes`返回`local-dev`为HEALTHY，包含Mongo接收心跳、原始Worker时刻、校准遥测时刻、实际网卡及约1秒采样窗口。独立代码审核无阻断项，最终Ruff及差异检查通过。

## 部署与尚未证实部分

必须更新API、各独立Worker及前端；旧Worker仍会写本机心跳，不能自动推算历史接收时间。目标公司环境应以相同采样时间和相同网卡集合对照MobaXterm；检查API新字段`networkInterfaces`、`networkSampleSeconds`及看板遥测范围。Docker模板已配置只读宿主proc，实际容器是否按此部署需目标核查。排除容器桥接/veth避免常见重复计数，复杂bond/VLAN拓扑仍应现场核对。当前本机验证不能替代公司网络实际计数比对或物理多机长期验收。
