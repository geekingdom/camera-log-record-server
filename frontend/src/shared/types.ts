// 前后端共享的数据契约。状态保留 string 扩展，兼容服务端后续增加状态而不阻断列表渲染。
export type Protocol = "SSH" | "TELNET_DEVICE" | "TELNET_SERIAL";
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
  version?: number;
  description?: string;
  deviceId?: string;
  sourceTemplateId?: string | null;
  sourceTemplateVersion?: number | null;
  encoding?: string;
  loginPrompt?: string;
  passwordPrompt?: string;
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
export interface CommandExecution {
  id?: string;
  command: string;
  status?: string;
  createdAt?: string;
  output?: string;
  [key: string]: unknown;
}
