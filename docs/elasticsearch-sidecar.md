# Elasticsearch 独立旁路索引

## 数据合同

原始日志和小时归档仍是权威数据源。采集 Worker 不连接或等待 ES；每个节点运行一个独立索引进程，以只读方式访问原始日志，按目录中的 READY 文件逐批回放。尚未封存、索引失败、超过热保留期或 ES 不可用的文件继续使用原文检索，页面展示覆盖水位，不能把未知覆盖解释为没有日志。

索引保存 32 KiB 文本块（16 字节重叠）及文件、任务、资源、节点、运行、会话、字节位置和接收时间。接收时间来自原始旁路时间索引；旧文件缺少时间索引时保留 null，不伪造精确接收时间。最终命中仍由原文逐行确认，返回的原始文件位置、上下文及下载行为保持不变。关键词保持大小写敏感的字面匹配；ES wildcard 字段只筛选前三字符候选，并转义通配字符。短关键词、跨分卷行和不完整证据保守回退，因此最坏情况下仍需原文扫描，不能承诺所有查询恒定耗时。

来源版本由文件身份、大小和 SHA-256 生成。每块以来源版本和字节偏移生成稳定 ID，bulk 每项确认成功后才提交 SQLite WAL 检查点；部分成功可安全重放。只有原始摘要和 ES 块数均核验通过后才发布覆盖证明。查询还会核对块数，缺块立即撤销阴性证据。重建时保留原始归档，停止索引器后可删除其检查点/指定 ES 索引，再重新启动；重复写入不会新增重复结果。禁止删除原始日志来“重建”。

## 部署

默认 `ELASTICSEARCH_URL` 留空，维持现有检索方式。API、所有执行检索作业的 Worker 和独立索引器必须指向同一 ES 索引。公司已有 ES 时优先使用已有集群，通过 TLS 和 API Key 授权；浏览器不连接 ES。

可选单节点启动示例为 `deploy/elasticsearch.yml`：设置 `ELASTIC_PASSWORD` 后执行 `docker compose -f deploy/elasticsearch.yml up -d`。此模板仅绑定宿主机回环 9200，默认不开放远程访问。跨主机应部署有证书和访问控制的 ES 地址，不能把 localhost 填给其他主机。Docker 桥接网络内也不能用 localhost 指向宿主机。ES 8 默认宿主机要求 `vm.max_map_count` 至少 262144，请按 ES 运维要求配置。

通过 ES 官方 `POST /_security/api_key` 创建密钥，填入响应的 `encoded` 值。索引器所需权限为指定索引上的 `create_index/read/write/maintenance`（创建、读取、批写/删除和刷新）；日常不执行删除索引，重建可由管理员单独授权。API 与执行检索的 Worker 只需要 `read`。完整部署共享环境文件时可共用索引器密钥，但应限制网络来源和索引名，不应授予全集群管理员权限。

```dotenv
ELASTICSEARCH_URL=https://your-internal-es:9200
ELASTICSEARCH_API_KEY=填入实际encoded密钥
ELASTICSEARCH_VERIFY_TLS=true
ELASTICSEARCH_INDEX=camera-logs-v1
ELASTICSEARCH_TIMEOUT_SECONDS=5
ELASTICSEARCH_RETENTION_DAYS=7
ELASTICSEARCH_READ_MIB_PER_SECOND=8
ELASTICSEARCH_BATCH_CHUNKS=32
ELASTICSEARCH_POLL_SECONDS=30
ELASTICSEARCH_FILE_PAUSE_SECONDS=0.1
```

`deploy-all.sh` / `deploy.sh worker` 在 Worker 部署通过后派生独立的 `<Worker项目名>-indexer` Compose 项目，复用实际 Worker 镜像与网络，日志挂载只读，检查点使用独立命名卷。默认上限 1 CPU / 512 MiB；索引器失败只给出警告，不回滚已恢复的采集。修改配置后重新部署，清空 URL 会停止该派生项目而保留检查点卷。原生部署使用 `python -m camera_logs.indexing.runner`，`--once` 用于单轮补建；索引失败记入日志并在下一轮重试，不代表所有源已覆盖，应检查覆盖接口。

## 容量、权限和删除

ES 是额外正文副本，不能沿用“设备正文完全不入库”的描述。MongoDB 仍管理任务、权限、目录和现有有界检索作业结果；SQLite 只保存确认位置。API 依旧执行 `logs:read` 和任务范围鉴权，未经授权无法通过覆盖接口或历史检索读取其他任务。禁止将 ES REST 端口暴露给平台普通用户。

默认热保留 7 天，从原始日志小时计算，重建不会延长保留期。每轮开始删除本节点过期正文/证明，并对已删除或缺失的 Mongo 文件目录补偿删除全部 ES 文档；Mongo 不可用时拒绝猜测删除，恢复后补偿。暂停/软删除任务仍按原始日志保留期提供历史检索，不立即误删原文。ES 停机期间删除会延后，恢复后先清理再补建。

单文件约每 32 KiB 一条文本记录加一条覆盖证明；实际容量包含 ES wildcard、倒排索引和原文副本，不能按 gzip 后的归档大小估算。默认建 1 分片、0 副本适合本机验证；生产多机应由 ES 管理员预建同名索引，按容量设置分片、副本和磁盘水位。提高副本后应单独预算磁盘。索引可重建不等于 ES 高可用。

索引读取默认 8 MiB/s，与 Worker 写入预算隔离。同一个小时包跨目录分页汇总后只顺序解压一次，预算包含跳过的逻辑字节；单包目录超过4096成员时明确记录失败并保持原文回退，不无限展开内存。首次补建大量旧归档仍有磁盘/解压成本；应先小范围验证实际硬盘吞吐，再调整读取预算。一天 600 MiB/h 的极端设备约 14 GiB 原始正文，7 天约 98 GiB，额外 ES 容量应实测后估算，不使用压缩包大小做容量承诺。
