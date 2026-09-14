# 管理事件游标验收

## 范围

审计、运行和请求事件新增显式游标分页，原页码接口保留。游标首屏传空字符串；默认只读取每页数量加一个探测项，不统计总数。用户点击统计时才计算完整筛选集的总数。

## 真实数据库证据

`scripts/verify_event_cursor.py`仅使用随机隔离库并在finally删除，不访问设备或真实日志。245条相同时间的ObjectId和字符串主键记录（包含24位十六进制字符串）跨10页与Mongo原始排序完全一致；同时覆盖派生PENDING、legacy detectedAt、跨条件拒绝和显式总数。

第五页使用真实opaque cursor调用正式查询器，代理捕获实际filter/sort/limit后进行Explain：目标集合查询一次，返回26个候选，API展示25条，剩余一条用于hasMore；keysExamined=26、docsExamined=26。winning plan为IXSCAN有序合并，无阻塞SORT。本证据不代表生产数据分布、低选择性派生条件或长期容量验收。

诊断更正：初版验证脚本递归整个Explain，误将rejectedPlans中的SORT认定为实际阻塞排序。正确计划原本已采用SORT_MERGE。已修正为仅判断winningPlan，撤回因此尝试的多请求分段方案，保留单查询typed-OR。随后增加查询次数与候选数量断言，防止只记录最后一个分支的Explain而漏算总成本。

## 浏览器边界

`verify_audit_browser.py`在随机库启动独立API/Vite，注入55条合成审计记录，结束回收进程、数据库和临时目录。`browser_audit_workspace.mjs`验证真实Cookie、三类事件、游标前后页、翻页失败保留与重试、未提交筛选隔离、按需总数，并保存1440/390截图。迟到统计需同时隔离结果与重置加载状态；不以数据未污染替代按钮可继续使用的验证。

## 持续验证

真实游标脚本已加入完整容器CI，测试命令以stdin传入API容器；不要求公司内部设备访问。全项目状态及最终测试数量以`../implementation-status.md`为准。
