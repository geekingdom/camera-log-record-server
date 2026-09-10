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
/** 海康网络设备认证历史的安全结果分类。 */
export type AuthenticationRecordResult = "SUCCESS" | "AUTH_FAILED" | "OFFLINE" | "ERROR";
/** 单次认证仅保存身份摘要和安全提示，不包含认证凭据或设备原始响应。 */
export interface AuthenticationRecord {
  id: string;
  createdAt?: string;
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
export interface Task {
  id: string;
  name: string;
  protocol: Protocol;
  ip?: string;
  port?: number;
  username?: string;
  password?: string;
  enableCoredumpMonitor?: boolean;
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
