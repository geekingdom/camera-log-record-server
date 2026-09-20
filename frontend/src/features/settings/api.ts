// 平台设置接口保持在功能目录，复用共享请求边界的认证和错误解析。
import { idempotencyKey, request } from "../../shared/api";

/** 设备指标规则由服务端校验正则、输出单位和命令边界。 */
export interface ResourceMonitorItem {
  id: string;
  name: string;
  command: string;
  pattern: string;
  unit: "KB" | "%";
  enabled: boolean;
}

export interface ResourceMonitorProcessRule {
  id: string;
  name: string;
  pattern: string;
  nameGroup?: number | null;
  enabled: boolean;
}

export interface ResourceMonitorConfig {
  intervalSeconds: number;
  retentionDays: number;
  items: ResourceMonitorItem[];
  processDiscoveryCommand: string;
  processRules: ResourceMonitorProcessRule[];
  processStatusCommand: string;
  processValuePattern: string;
}

/** 增长型数据库记录的管理员保留策略；0 明确表示不自动归档或清理。 */
export interface RecordRetentionConfig {
  auditDays: number;
  eventDays: number;
  runDays: number;
}

export interface PlatformSettings {
  retentionDays: number;
  clusterCapacity?: number;
  liveLogBufferMiB: number;
  version: number;
  updatedAt: string;
  resourceMonitor?: ResourceMonitorConfig;
  recordRetention?: RecordRetentionConfig;
}

/** 已登录用户可读取的实时页面显示约束，不包含管理员平台配置。 */
export interface DisplaySettings {
  liveLogBufferMiB: number;
}

/** 节点采集性能策略；AUTO 的数值是节点自动调节时可使用的资源上限。 */
export interface PerformancePolicy {
  mode: "AUTO" | "MANUAL";
  bufferMiB: number;
  writeConcurrency: number;
  batchKiB: number;
  flushIntervalMs: number;
  readMiBPerSecond: number;
}

export interface NodeConfig {
  id: string;
  url: string;
  capacity: number;
  accepting: boolean;
  inputRateLimitMiB?: number;
  writeLatencyLimitMs?: number;
  version: number;
  registered: boolean;
  online: boolean;
  reportedAt: string | null;
  reportedUrl?: string;
  urlMismatch?: boolean;
  activeTasks?: number;
  diskPercent?: number | null;
  isGeneralNode?: boolean;
  resourceNetworks?: string[];
  performance: PerformancePolicy;
  performanceStatus?: Record<string, unknown> | null;
}

export interface NodeRegistration {
  id: string;
  url: string;
  capacity: number;
  accepting: boolean;
  inputRateLimitMiB?: number;
  writeLatencyLimitMs?: number;
  isGeneralNode?: boolean;
  resourceNetworks?: string[];
  performance?: PerformancePolicy;
}

export const settingsApi = {
  platform: () => request<PlatformSettings>("/platform-settings"),
  display: () => request<DisplaySettings>("/display-settings"),
  updatePlatform: (body: Pick<PlatformSettings, "retentionDays" | "version"> & { clusterCapacity?: number; liveLogBufferMiB?: number; resourceMonitor?: ResourceMonitorConfig; recordRetention?: RecordRetentionConfig }) =>
    request<PlatformSettings>("/platform-settings", {
      method: "PATCH",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(body),
    }),
  nodes: () => request<{ items: NodeConfig[] }>("/admin/nodes"),
  deleteNode: (id: string, version: number) =>
    request<void>(`/admin/nodes/${encodeURIComponent(id)}?version=${version}`, { method: "DELETE" }),
  registerNode: (body: NodeRegistration) =>
    request<NodeConfig>("/admin/nodes", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(body),
    }),
  updateNode: (id: string, body: Pick<NodeConfig, "version" | "capacity" | "accepting" | "isGeneralNode" | "resourceNetworks" | "inputRateLimitMiB" | "writeLatencyLimitMs" | "performance">) =>
    request<NodeConfig>(`/admin/nodes/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(body),
    }),
};
