"""日志目录、下载和检索接口的公开响应字段合同。"""

from camera_logs.common.api_contracts.response_schema import array, field, obj, page

FILE_DEPLOYMENT_CLOSED_PROOF = obj("部署关闭后用于恢复原始分卷的精确证明。", {
    "token": field("string", "同次受控部署恢复的内部匹配令牌。"),
    "nodeId": field("string", "已停止的原采集节点标识。"),
    "taskId": field("string", "原采集任务标识。"), "runId": field("string", "原采集运行标识。"),
    "generation": field("integer", "原采集运行世代。"), "sessionId": field("string", "原采集会话标识。"),
    "confirmedAt": field("string", "外部确认原容器已停止的时间。"),
    "fileId": field("string", "待恢复原始分卷的文件标识。", nullable=True),
    "path": field("string", "待恢复原始分卷在节点日志根目录内的路径。", nullable=True),
    "createdAt": field("string", "目录首次登记该原始分卷的时间。", nullable=True),
    "watermark": field("integer", "关闭确认时已登记的原始分卷字节水位，单位字节。", nullable=True),
    "firstSequence": field("integer", "该原始分卷的首个接收序号。", nullable=True),
    "archiveRecoveredAt": field("string", "已按本证明重新登记归档的时间；尚未恢复时省略。", nullable=True),
}, ())

LOG_FILE = obj("日志分卷公开目录项。", {
    "id": field("string", "文件标识。"), "taskId": field("string", "所属任务。"),
    "runId": field("string", "采集运行标识。"), "sessionId": field("string", "采集会话标识。"),
    "nodeId": field("string", "保管节点。"),
    "status": field("string", "目录状态：OPEN 表示仍在写入，READY 表示已校验归档可读，DELETING 表示保留清理已领取；兼容历史 DELETED 值时文件已不可读。"),
    "bytes": field("integer", "当前冻结的原始日志字节数，单位字节；不小于 0。"),
    "hour": field("string", "归属的上海逻辑小时起点；响应中以带 +08:00 的 ISO 8601 时间表示。"),
    "archiveName": field("string", "READY 分卷所在归档文件名；OPEN 分卷为 null 或省略。", nullable=True),
    "archiveMember": field("string", "READY 分卷在共享归档内的成员文件名；新归档按实际服务器首末接收时间冻结，历史归档可能保留逻辑小时成员名；OPEN 分卷为 null 或省略。", nullable=True),
    "rawFileName": field("string", "OPEN 时为当前临时原始分卷名；READY 时为兼容旧客户端保留的片段展示名并与 archiveMember 一致，物理源文件名和路径不公开。", nullable=True),
    "segmentNumber": field("integer", "同小时内片段序号。", nullable=True),
    "firstSequence": field("integer", "首写入序号。", nullable=True),
    "lastSequence": field("integer", "末写入序号。", nullable=True),
    "firstReceivedAt": field("string", "服务器首次收到本分卷日志的绝对时间；响应以北京时间 ISO 8601 表示。", nullable=True),
    "lastReceivedAt": field("string", "服务器最后收到本分卷日志的绝对时间；OPEN 目录存在该值时表示当前已观测到的末时间，READY 后固定，响应以北京时间 ISO 8601 表示。", nullable=True),
    "runStartedAt": field("string", "采集运行开始时间；旧目录可能省略。", nullable=True),
    "sessionStartedAt": field("string", "采集会话开始时间；旧目录可能省略。", nullable=True),
    "createdAt": field("string", "目录记录首次写入时间；旧目录可能省略。", nullable=True),
    "updatedAt": field("string", "目录记录最近刷新时间；旧目录可能省略。", nullable=True),
    "archiveBytes": field("integer", "所属归档文件的物理字节数，单位字节；共享归档的成员会返回相同值。", nullable=True),
    "sha256": field("string", "完整归档的 SHA-256 校验值。", nullable=True),
    "archiveGroupId": field("string", "共享归档成员组标识。", nullable=True),
    "closedAt": field("string", "分卷关闭时间。", nullable=True),
    "deletedAt": field("string", "目录删除时间。", nullable=True),
    "retainUntil": field("string", "下载作业对该分卷设置的最晚保留时间；在此时间前保留清理不得删除归档。", nullable=True),
    "retentionClaimedAt": field("string", "保留清理已原子领取该分卷的时间；状态为 DELETING 时出现。", nullable=True),
    "retentionErrorAt": field("string", "最近一次物理归档清理失败的时间；仅 DELETING 重试时出现。", nullable=True),
    "deploymentClosedProof": FILE_DEPLOYMENT_CLOSED_PROOF | {"nullable": True},
}, ("id",))

SEARCH_RESULT = obj("日志检索命中。", {
    "fileId": field("string", "来源文件。"), "offset": field("integer", "命中偏移。"),
    "lineStartFileId": field("string", "整行起始文件。"),
    "lineStartOffset": field("integer", "整行起始偏移。"), "text": field("string", "命中行文本。"),
    "encodingError": field("boolean", "命中行按 UTF-8 解码时是否发生替换；为 true 时 text 含替换字符，原始字节仍在日志文件中。", nullable=True),
}, ("fileId", "offset", "text"))

TIME_RANGE_RESULT = obj("无关键词时间范围查询返回的原始日志字节片段。", {
    "fileId": field("string", "来源文件标识。"), "offset": field("integer", "该片在来源文件内的起始字节偏移。"),
    "length": field("integer", "本片原始字节长度，最大 4096 字节。"),
    "receivedAt": field("string", "该片归属的服务器接收时间。"),
    "data": field("string", "本片原始字节的 Base64 编码；按分页顺序解码拼接可恢复查询范围内原始内容。"),
    "text": field("string", "本片按 UTF-8 替换解码的预览文本；不应用于回写原始日志。"),
}, ("fileId", "offset", "length", "receivedAt", "data", "text"))

SEARCH_RESULT_ITEM = {"oneOf": [SEARCH_RESULT, TIME_RANGE_RESULT], "description": "关键词检索命中行，或无关键词时间范围查询的原始字节片段。"}

ARTIFACT_LOG_SEGMENT = obj("实际写入下载制品的单个冻结日志分卷；数组顺序仅为制品写入顺序，不表示其时间连续。", {
    "fileId": field("string", "冻结日志分卷标识。"),
    "hour": field("string", "该分卷所属的逻辑小时目录标识。"),
    "firstReceivedAt": field("string", "该分卷实际首条日志的服务器接收时间；历史分卷未保存时为 null。", nullable=True),
    "lastReceivedAt": field("string", "该分卷实际末条日志的服务器接收时间；历史分卷未保存时为 null。", nullable=True),
}, ("fileId", "hour"))

JOB = obj("异步下载或检索作业公开投影。", {
    "id": field("string", "作业标识。"), "kind": field("string", "DOWNLOAD 或 SEARCH。"),
    "status": field("string", "作业状态：QUEUED 等待节点领取，RUNNING 正在执行，SUCCEEDED 已成功生成结果，FAILED 执行失败，CANCELLED 已取消，EXPIRED 在执行期限内未完成。"),
    "progress": field("integer", "执行进度百分比，范围 0 至 100；终态成功为 100。"),
    "actor": field("string", "提交者标识。"), "createdBy": field("string", "下载任务创建人标识快照；旧作业可能为空。", nullable=True),
    "createdByName": field("string", "下载任务创建人展示名快照；后续改名或删除不回写，旧作业可能为空。", nullable=True), "taskId": field("string", "所属任务。"),
    "nodeId": field("string", "处理节点。"), "taskName": field("string", "冻结任务名。"),
    "taskIp": field("string", "冻结任务 IP。"), "createdAt": field("string", "创建时间。"),
    "expiresAt": field("string", "下载制品到期时间；检索作业或尚未分配制品保留期时为 null 或省略。", nullable=True),
    "updatedAt": field("string", "作业最近一次持久化状态更新时间；旧作业可省略或为 null。", nullable=True),
    "startedAt": field("string", "Worker 成功领取作业并进入 RUNNING 的时间。", nullable=True),
    "completedAt": field("string", "作业进入 SUCCEEDED、FAILED、CANCELLED 或 EXPIRED 终态的时间。", nullable=True),
    "executionExpiresAt": field("string", "执行到期时间。", nullable=True),
    "hourIds": array("下载作业规范化后的上海逻辑小时目录；检索作业可省略。", field("string", "小时目录标识。"), nullable=True),
    "allowPartial": field("boolean", "仅下载作业使用：true 允许所选小时缺少可用分卷，false 时缺失即拒绝创建。"),
    "files": array("创建时冻结的分卷目录；includeResults=false 时省略。", LOG_FILE),
    "keyword": field("string", "检索关键词。", nullable=True), "start": field("string", "检索起始时间。", nullable=True),
    "end": field("string", "检索结束时间。", nullable=True), "results": array("检索结果。", SEARCH_RESULT_ITEM),
    "resultCount": field("integer", "已确认命中数。"), "truncated": field("boolean", "结果是否截断。"),
    "filename": field("string", "产物文件名。", nullable=True), "bytes": field("integer", "产物字节数。", nullable=True),
    "artifactCreatedAt": field("string", "产物创建时间。", nullable=True),
    "artifactRetentionDays": field("integer", "产物保留天数。", nullable=True),
    "artifactLogSegmentCount": field("integer", "实际写入下载制品的日志分卷总数；旧作业没有此快照时省略。", minimum=0),
    "artifactLogSegments": array("实际写入下载制品的离散日志分卷及各自接收时间；未成功生成制品或旧作业时为空或省略，不能把数组首尾解释为连续覆盖范围。", ARTIFACT_LOG_SEGMENT, nullable=True),
    "outputLockProtocol": field("integer", "输出制品写入与清理锁协议版本；Worker 已领取输出时出现，旧作业可省略。"),
    "missing": array("成功的部分下载中未能读取的冻结文件标识；非部分下载或尚未完成时省略。", field("string", "未能读取的文件标识。")),
    "error": field("string", "失败或取消错误码。", nullable=True), "errorMessage": field("string", "失败说明。", nullable=True),
}, ("id", "kind", "status", "taskId"))

DOWNLOAD_HISTORY = obj("下载历史行，不含冻结文件目录。", {
    "id": field("string", "作业标识。"), "kind": field("string", "固定为 DOWNLOAD。"),
    "taskId": field("string", "任务标识。"), "nodeId": field("string", "处理节点标识。", nullable=True),
    "status": field("string", "下载作业状态：QUEUED、RUNNING、SUCCEEDED、FAILED、CANCELLED 或 EXPIRED。"),
    "createdAt": field("string", "创建时间。"),
    "createdBy": field("string", "下载任务创建人标识快照；旧作业可使用已保存的actor标识，均未保存时为null或省略。", nullable=True),
    "createdByName": field("string", "下载任务创建人展示名快照；后续改名或删除不回写，旧作业未保存时为 null。", nullable=True),
    "updatedAt": field("string", "最近处理时间。", nullable=True), "expiresAt": field("string", "到期时间。", nullable=True),
    "artifactCreatedAt": field("string", "产物创建时间。", nullable=True), "artifactRetentionDays": field("integer", "产物保留天数。", nullable=True),
    "filename": field("string", "产物文件名。", nullable=True), "bytes": field("integer", "产物字节数。", nullable=True),
    "progress": field("integer", "执行进度。"), "error": field("string", "失败或取消错误码。", nullable=True),
    "artifactLogSegmentCount": field("integer", "实际写入下载制品的日志分卷总数；旧作业没有此快照时省略。", minimum=0),
    "artifactLogSegments": array("实际写入制品的前8个日志分卷预览；总数由artifactLogSegmentCount给出，完整数组通过GET /api/v1/downloads/{id}按需读取。每项独立时间不能合并解释为连续覆盖；旧作业或尚未成功时为null或省略。", ARTIFACT_LOG_SEGMENT, nullable=True),
    "downloadAvailable": field("boolean", "产物当前是否可下载。"),
}, ("id", "kind", "taskId", "status", "createdAt", "downloadAvailable"))

HOUR = obj("上海小时日志归档摘要。", {
    "hourId": field("string", "小时目录标识。"), "hour": field("string", "小时起点。"),
    "status": field("string", "小时可用状态：READY 表示全部成员已归档，OPEN 表示至少一个成员仍在写入，UNAVAILABLE 表示至少一个成员不可读或缺失。"),
    "integrity": field("string", "小时完整性：VERIFIED 表示全部成员有 SHA-256，UNVERIFIED 表示成员已归档但至少一个缺少校验值，OPEN 表示仍有写入成员，UNAVAILABLE 表示有不可用成员。"),
    "files": array("该小时全部公开分卷目录。", LOG_FILE), "bytes": field("integer", "原始字节数。"),
    "archiveBytes": field("integer", "归档物理字节数。"), "fragmentCount": field("integer", "分卷总数。"),
    "readyCount": field("integer", "READY 分卷数。"), "openCount": field("integer", "OPEN 分卷数。"),
    "unavailableCount": field("integer", "不可读分卷数。"),
}, ("hourId", "hour", "status", "integrity", "files", "bytes", "archiveBytes", "fragmentCount", "readyCount", "openCount", "unavailableCount"))

INDEX_STATUS = obj("全文候选索引覆盖状态。", {
    "enabled": field("boolean", "是否配置 Elasticsearch 候选索引服务。false 时仍可回退扫描原始日志。"),
    "status": field("string", "候选索引查询状态；仅 status=AVAILABLE 且未截断时可据 coverageComplete 判断完整覆盖。"),
    "totalFiles": field("integer", "查询范围内参与覆盖计算的 OPEN 或 READY 分卷数，最多 10000。"),
    "coveredFiles": field("integer", "有可验证候选索引证据的参与分卷数，不大于 totalFiles。"),
    "coverageComplete": field("boolean", "仅当 status=AVAILABLE、未截断且每个参与分卷均有索引证据时为 true。"),
    "latestIndexedHour": field("string", "最新已索引小时。", nullable=True),
    "contiguousCoveredThrough": field("string", "连续覆盖水位。", nullable=True),
    "truncated": field("boolean", "参与覆盖计算的目录是否超过 10000 个分卷；true 时不声称覆盖完整。"),
    "fallback": field("string", "当前固定为 RAW_SCAN，表示索引缺失或不足时逐分卷扫描权威原始日志。"),
}, ("enabled", "status", "totalFiles", "coveredFiles", "coverageComplete", "truncated", "fallback"))

LOG_GAP = obj("日志缺口目录。", {
    "items": array("可补读区间。", obj("可补读片段。", {
        "fileId": field("string", "文件标识。"), "sessionId": field("string", "会话标识。"),
        "start": field("integer", "起始偏移。"), "end": field("integer", "结束偏移。"),
    }, ("fileId", "sessionId", "start", "end"))),
    "nextCursor": field("string", "下一页游标。", nullable=True),
    "unrecoverable": array("不可恢复缺口。", obj("缺口原因。", {
        "reason": field("string", "稳定原因码。"), "message": field("string", "中文说明。"),
    }, ("reason", "message"))),
}, ("items", "nextCursor", "unrecoverable"))

SEARCH_RESULTS_PAGE = obj("检索结果页。", {
    "items": array("命中记录。", SEARCH_RESULT_ITEM), "total": field("integer", "命中总数。"),
    "page": field("integer", "页码。"), "pageSize": field("integer", "页大小。"),
    "status": field("string", "检索作业状态。"), "truncated": field("boolean", "是否截断。"),
}, ("items", "total", "page", "pageSize", "status", "truncated"))

LOG_HOURS_PAGE = page(HOUR)
