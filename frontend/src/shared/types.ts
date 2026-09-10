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
  createdBy?: string;
  createdByName?: string;
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
  writeLatencyMs: number;
  writeLatencySamples: number;
  writeLatencyPendingMs: number;
  writeLatencyWindowSeconds: number;
  [key: string]: unknown;
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
  command: string;
  status?: string;
  createdAt?: string;
  output?: string;
  [key: string]: unknown;
}
