# 总表核对与开发副本清理

基线 `beca86f`。本记录是历史证据，当前业务顺序见 [总表](../implementation-status.md)。

## 重新验证的事实

- 祖先提交 `92e01d6` 和 `commands/reservation.py` 已用 Mongo 事务原子预留预算与发送记录；设备 socket 不属于事务。
- 本机显式构造 `httpx.Limits(max_connections=64)` 返回 `max_keepalive_connections=None`，不能用 20 构建诊断假设。
- `logs/compression.py` 校验归档后发布、同步目录，随后删除源分卷。
- `WriteLatency` 按批次测接收到写完；`BatchLatency` 按行测源发送到 API 读齐，溢出区间为保守上界。两者不等价。

## 依赖与历史样本

起始工作区已有 `sniffio>=1.3.1,<2`。显式依赖用于 HTTPcore 异步环境识别，避免依赖缺失的导入扫描；本轮 `pip check` 通过。
重新读取 `.local/service-node-diagnostic-64-sniffio/report.json`：4,608,000 行，完整性与收尾通过；全路重叠 52.09 秒，API 可读 P99 上界 1246.685ms，`passed=false`。本轮没有重跑这次压测，也不据此承诺性能达标。

## 清理与限制

- 删除 1,028 个已验证压测下载副本，合计 601,373,134 字节；重复预览 0 个候选。报告和摘要保留。
- `du -sh .local` 从约 3.6G 降为 3.0G；`output` 约 198M 未清理。
- 2026-09-09 01:30 UTC 数据库只读检查：两路真实任务为 COLLECTING，无 QUEUED/RUNNING 日志作业；已软删除回环协议压测资源关联 4,721 条文件记录，其中 3,746 条 retainUntil 尚未到期。此数量仅为调查，不是允许删除的身份清单。
- 服务端文件、真实设备日志、失败实验和浏览器产物未删除，不改变全局保留期。

## 本轮验证

- `pytest -q`：536 passed，357 条既有依赖弃用警告。
- `ruff check backend tests scripts`、`git diff --check`、`pip check`：通过。
- 前端：25 项测试通过，TypeScript/Vite 构建通过。
- 无 UI 改动，本轮未重跑浏览器截图；既有截图不冒充新验收证据。
