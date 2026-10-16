// 前后端共享的数据契约。状态保留 string 扩展，兼容服务端后续增加状态而不阻断列表渲染。
export type Protocol = "SSH" | "TELNET_DEVICE" | "TELNET_SERIAL";
export type ResourceKind = "HIKVISION_NETWORK" | "SERIAL_SERVER";
export type ResourceAuthType = "DIGEST" | "BASIC";
export interface ResourceAuthentication {
  model?: string;
  subSerialNumber?: string;
  softwareVersion?: string;
}
export interface Resource extends ResourceAuthentication {
  id: string;
  name: string;
  kind: ResourceKind;
  ip: string;
  username?: string;
  authType?: ResourceAuthType;
  /** 资源级 Coredump 采集开关，仅海康网络设备可用。 */
  enableCoredumpMonitor?: boolean;
  /** 资源目录聚合出的 Coredump 文件存在标识，仅表示当前存在文件，不是未读状态。 */
  hasCoredumps?: boolean;
  /** 资源级 CPU 和内存采样开关，仅海康网络设备可用。 */
  enableResourceMonitor?: boolean;
  version?: number;
  healthStatus?: "ONLINE" | "AUTH_FAILED" | "OFFLINE" | "ERROR" | string;
  healthCheckedAt?: string;
  deletedAt?: string | null;
  createdAt?: string;
  taskCount?: number;
  activeTaskCount?: number;
  unsettledTaskCount?: number;
  createdBy?: string;
  createdByName?: string;
}
/** 资源监控样本内的单项 CPU 或内存值；pid 仅兼容历史数据，进程对象按名称识别。 */
export interface ResourceMetricValue {
  id: string;
  name: string;
  value: number;
  unit: "KB" | "%";
  pid?: number;
}
/** 一次采样只保存结构化数值和受限错误码，不包含设备命令正文。 */
export interface ResourceMetricSample {
  sampledAt: string;
  status: string;
  values: ResourceMetricValue[];
  errorCode?: string | null;
}
/** 资源监控历史采用游标续页，避免长时间范围计算精确总数。 */
export interface ResourceMetricsPage {
  items: ResourceMetricSample[];
  nextCursor?: string | null;
  owner?: { id?: string; name?: string } | null;
}
/** 资源看板按上海自然日汇总，不携带日志正文或节点敏感诊断。 */
export interface DashboardDailyMetric {
  date: string;
  bytes: number;
  fileCount: number;
}
export interface DashboardResourceRank {
  resourceId: string;
  name: string;
  ip: string;
  bytes: number;
  fileCount: number;
}
export interface DashboardNodeHealth {
  id: string;
  name: string;
  healthLevel: string;
  reasons: string[];
  accepting: boolean;
  activeTasks: number;
  capacity: number;
  inputBytesPerSecond: number;
  lastHeartbeat: string | null;
}
export interface DashboardCoredumpItem {
  resourceId: string;
  name: string;
  ip: string;
  fileCount: number;
  bytes: number;
  lastReceivedAt: string | null;
}
export interface DashboardSnapshot {
  /** 服务端成功生成该快照的 UTC 时刻，用于页面更新时间而非浏览器收包时间。 */
  generatedAt: string;
  date: string;
  days: 7 | 14 | 30;
  range: { startDate: string; endDate: string };
  overview: { resources: number; onlineResources: number; collectingTasks: number; tasks: number; nodes: number | null };
  availability: { logs: boolean; nodes: boolean };
  /** 无日志读取权限时服务端明确返回 null，而不是将受限统计伪装为零。 */
  daily: DashboardDailyMetric[] | null;
  topResources: DashboardResourceRank[] | null;
  topResourcesTruncated: boolean | null;
  coredumps: { resourceCount: number; fileCount: number; bytes: number; items: DashboardCoredumpItem[]; truncated: boolean } | null;
  /** 普通账号不接收节点标识、健康原因或节点数量。 */
  nodes: DashboardNodeHealth[] | null;
  notes?: string[];
}
/** 海康网络设备认证历史的安全结果分类。 */
export type AuthenticationRecordResult = "SUCCESS" | "AUTH_FAILED" | "OFFLINE" | "ERROR";
/** 单次认证仅保存身份摘要和安全提示，不包含认证凭据或设备原始响应。 */
export interface AuthenticationRecord {
  id: string;
  /** 聚合区间首次认证时间；旧单次记录与 createdAt 相同。 */
  createdAt?: string;
  /** 连续相同成功状态最后一次被认证观察到的时间。 */
  latestAt?: string;
  /** 同日连续相同周期认证结果的观测次数，成功和失败均累计；旧记录默认按 1 展示。 */
  occurrenceCount?: number;
  source?: string;
  result: AuthenticationRecordResult | string;
  modelBefore?: string | null;
  modelAfter?: string | null;
  serialBefore?: string | null;
  serialAfter?: string | null;
  identityChanged?: boolean;
  initialAuthentication?: boolean;
  message?: string | null;
}
/** 游标读取不计算总数；空 cursor 表示请求满足筛选条件的首屏。 */
export interface CursorPage<T> {
  items: T[];
  total: null;
  pageSize: number;
  hasMore: boolean;
  nextCursor: string | null;
}
/** 同一资源当前实际执行 NFS Coredump 监控的采集任务。 */
export interface CoredumpMonitorStatus {
  active: boolean;
  ownerTask: { id: string; name: string } | null;
  mountStatus: string | null;
}
export type TaskActualStatus =
  | "STOPPED"
  | "CONNECTING"
  | "COLLECTING"
  | "RECONNECTING"
  | "PAUSED"
  | string;
export type TaskDesiredState = "STOPPED" | "RUNNING" | "PAUSED" | string;
export interface InitialCommand {
  command: string;
  newline: "\n" | "\r" | "\r\n";
  delaySeconds: number;
  prompt: string | null;
  timeoutSeconds: number;
}
export interface ScheduledCommand {
  id?: string;
  command: string;
  totalExecutions: number;
  intervalSeconds: number;
}
/** 当前运行中定时命令的独立预算，按配置 ID 而非正文识别。 */
export interface ScheduledCommandProgress extends ScheduledCommand {
  id: string;
  attempts: number;
}
/** SSH 日志来源：主机或设备公开的三个从机通道。 */
export type SshTarget = "HOST" | "SLAVE_1" | "SLAVE_2" | "SLAVE_3";
export interface Task {
  id: string;
  name: string;
  protocol: Protocol;
  sshTarget?: SshTarget;
  ip?: string;
  port?: number;
  username?: string;
  password?: string;
  useResourceCredentials?: boolean;
  status?: TaskActualStatus;
  desiredState?: TaskDesiredState;
  error?: string | null;
  shellMode?: string;
  debugPhase?: string | null;
  commandBlocked?: boolean;
  nodeId?: string | null;
  debugError?: string | null;
  runId?: string;
  sessionId?: string;
  updatedAt?: string;
  createdAt?: string;
  version?: number;
  description?: string;
  sourceTemplateId?: string | null;
  sourceTemplateVersion?: number | null;
  resourceId: string;
  resourceDeleted?: boolean;
  resourceDeletedAt?: string;
  serialServerResourceId?: string | null;
  encoding?: string;
  loginPrompt?: string;
  passwordPrompt?: string;
  createdBy?: string;
  createdByName?: string;
  initialCommands: InitialCommand[];
  scheduledCommands: ScheduledCommand[];
}
export interface Template {
  id: string;
  name: string;
  description?: string;
  version?: number;
  initialCommands: InitialCommand[];
  scheduledCommands: ScheduledCommand[];
  createdBy?: string;
  createdByName?: string;
  sharedWith: string[];
  sharedWithAll: boolean;
  createdAt?: string;
  deletedAt?: string | null;
}
export interface Node {
  assessedAt?: string;
  id: string;
  name?: string;
  status?: string;
  address?: string;
  url?: string;
  heartbeat?: string;
  capacity?: number;
  activeTasks?: number;
  accepting?: boolean;
  isolated?: boolean;
  configurationMismatch?: boolean;
  diskPercent?: number;
  diskFreeBytes?: number;
  inputBytesPerSecond?: number;
  writeLatencyLimitMs?: number;
  writeLatencyMs?: number;
  writeLatencySamples?: number;
  writeLatencyPendingMs?: number;
  writeLatencyWindowSeconds?: number;
  /** Worker 根据节点性能策略生成的只读运行结论，不由浏览器推导。 */
  performanceStatus?: NodePerformanceStatus | null;
  telemetry?: NodeTelemetry;
  health?: NodeHealth;
}
export interface NodePerformanceStatus {
  state?: string;
  /** 当前采样是否仍满足CPU、内存或写入延迟压力条件。 */
  pressureActive?: boolean;
  /** 当前连续压力采样次数；达到后端阈值前不代表已经自动降速。 */
  pressureSamples?: number;
  /** 当前有效预算相对资源和管理员上限是否已被自动收紧。 */
  budgetReduced?: boolean;
  /** AUTO模式下用于比较的当前安全基准预算。 */
  baseline?: {
    bufferMiB?: number;
    writeConcurrency?: number;
    batchKiB?: number;
    flushIntervalMs?: number;
    readMiBPerSecond?: number;
  };
  /** 当前没有压力后持续稳定的秒数。 */
  recoveryStableSeconds?: number;
  /** 性能控制器要求暂停新任务准入；不影响已建立的采集会话。 */
  admissionBlocked?: boolean;
  effective?: {
    mode?: "AUTO" | "MANUAL" | string;
    bufferMiB?: number;
    writeConcurrency?: number;
    batchKiB?: number;
    flushIntervalMs?: number;
    readMiBPerSecond?: number;
  };
  reason?: string | null;
  changedAt?: string | null;
  resources?: Record<string, number | string | boolean | null>;
  buffer?: {
    reservedBytes?: number | null;
    limitBytes?: number | null;
    oldestPendingMs?: number | null;
    scope?: string | null;
  };
  writeIo?: {
    concurrency?: number;
    active?: number;
    waiting?: number;
    queue?: NodePerformanceStage;
    execution?: NodePerformanceStage;
    stages?: Record<string, NodePerformanceStage | undefined>;
  };
  /** 实际写入 tracker 的滑动窗口，不包含主动批等待，涵盖排队及 write_many 执行。 */
  writeIoLatency?: {
    p99Ms?: number;
    pendingMs?: number;
    samples?: number;
    windowSeconds?: number;
  };
  /** 归档压缩工作状态；旧 Worker 仅上报 pendingJobs 和 oldestSeconds。 */
  archives?: {
    pendingJobs?: number;
    /** 维护扫描发现的唯一持久归档目标数，可能包含尚未进入内存候选的 spool。 */
    diskPendingJobs?: number;
    waitingJobs?: number;
    runningJobs?: number;
    oldestSeconds?: number;
    oldestWaitingSeconds?: number;
    oldestRunningSeconds?: number;
  };
}
/** Worker 的写入阶段滚动统计；例如 `stages.batchWaitMs` 表示批次等待。 */
export interface NodePerformanceStage {
  samples?: number;
  sampleWindow?: number;
  p99Ms?: number;
}
/** Worker 上报的瞬时资源遥测；HOST 与 RUNTIME 范围由服务端明确标注。 */
export interface NodeTelemetry {
  sampledAt?: string | null;
  scope?: "HOST" | "RUNTIME" | "UNKNOWN" | string;
  status?: "OK" | "UNKNOWN";
  cpuPercent?: number | null;
  memoryUsedBytes?: number | null;
  memoryTotalBytes?: number | null;
  memoryPercent?: number | null;
  networkUploadBytesPerSecond?: number | null;
  networkDownloadBytesPerSecond?: number | null;
  networkInterfaces?: string[];
  networkSampleSeconds?: number | null;
  networkAccounting?: string;
}
/** 服务端按当前遥测计算的节点健康状态与可审阅原因。 */
export interface NodeHealth {
  status: "HEALTHY" | "WARNING" | "CRITICAL" | "UNKNOWN" | "OFFLINE" | string;
  reasons: string[];
}
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
}
export interface LogHour {
  hourId: string;
  hour: string;
  status: string;
  bytes: number;
  archiveBytes: number;
  fragmentCount?: number;
  readyCount?: number;
  openCount?: number;
  unavailableCount?: number;
  integrity?: string;
  files: LogFile[];
}
export interface LogFile {
  id: string;
  runId?: string;
  sessionId?: string;
  nodeId?: string;
  status: string;
  bytes: number;
  archiveBytes?: number;
  archiveName?: string;
  rawFileName?: string;
  sha256?: string;
  firstSequence?: number;
  lastSequence?: number;
}
export type CoredumpStatus = "RECEIVING" | "FREEZING" | "FROZEN" | string;
export interface CoredumpFile {
  id: string;
  resourceId: string;
  nodeId: string;
  name: string;
  size: number;
  receivedAt: string;
  firstSeenAt?: string;
  sourceModifiedAt?: string;
  sourceState?: "OBSERVING" | "CHANGING" | "STABLE";
  sourceStableAt?: string;
  sourceObservedAt?: string;
  sourceUnchangedSince?: string;
  updatedAt?: string;
  status: CoredumpStatus;
  version?: number;
}
export interface CoredumpExport {
  id: string;
  status: string;
  kind?: "COREDUMP_EXPORT" | string;
  createdAt?: string;
  expiresAt?: string;
  filename?: string;
  bytes?: number;
  etag?: string;
  error?: string;
}
/** 下载作业历史仅暴露审计与制品可用性字段，不包含节点路径或写入租约。 */
export interface DownloadHistoryItem {
  id: string;
  taskId: string;
  status: string;
  createdAt?: string;
  expiresAt?: string;
  filename?: string;
  bytes?: number;
  progress?: number;
  error?: string;
  /** 服务端在保留期和清理状态边界统一计算，前端不可自行推断。 */
  downloadAvailable: boolean;
}
/** 搜索索引覆盖度只描述候选加速能力；未覆盖范围仍由原文扫描保证结果正确性。 */
export interface SearchIndexStatus {
  enabled: boolean;
  status: "DISABLED" | "AVAILABLE" | "UNAVAILABLE" | string;
  totalFiles: number;
  coveredFiles: number;
  coverageComplete: boolean;
  latestIndexedHour?: string | null;
  contiguousCoveredThrough?: string | null;
  truncated?: boolean;
  fallback: "RAW_SCAN" | string;
}
export interface CommandExecution {
  id?: string;
  command?: string;
  commandId?: string;
  kind?: "MANUAL" | "SCHEDULED" | string;
  status?: string;
  createdAt?: string;
  output?: string;
  commandSource?: "SNAPSHOT" | "CURRENT_CONFIGURATION" | "UNAVAILABLE" | string;
  runId?: string | null;
  totalExecutions?: number;
  intervalSeconds?: number;
  [key: string]: unknown;
}
/** 命令记录分页响应补充当前运行与每条定时命令的预算进度。 */
export interface CommandExecutionPage extends Page<CommandExecution> {
  runId: string | null;
  scheduledCommands: ScheduledCommandProgress[];
}
