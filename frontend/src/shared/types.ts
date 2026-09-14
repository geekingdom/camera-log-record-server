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
/** 资源监控样本内的单项 CPU 或内存值；pid 仅用于区分同类进程。 */
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
/** 海康网络设备认证历史的安全结果分类。 */
export type AuthenticationRecordResult = "SUCCESS" | "AUTH_FAILED" | "OFFLINE" | "ERROR";
/** 单次认证仅保存身份摘要和安全提示，不包含认证凭据或设备原始响应。 */
export interface AuthenticationRecord {
  id: string;
  /** 聚合区间首次认证时间；旧单次记录与 createdAt 相同。 */
  createdAt?: string;
  /** 连续相同成功状态最后一次被认证观察到的时间。 */
  latestAt?: string;
  /** 连续相同成功状态合并后的观测次数；旧记录默认按 1 展示。 */
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
  writeLatencyMs?: number;
  writeLatencySamples?: number;
  writeLatencyPendingMs?: number;
  writeLatencyWindowSeconds?: number;
  telemetry?: NodeTelemetry;
  health?: NodeHealth;
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
