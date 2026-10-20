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
  serviceLogRetentionDays: number;
  version: number;
  updatedAt: string;
  resourceMonitor?: ResourceMonitorConfig;
  recordRetention?: RecordRetentionConfig;
}

/** 已登录用户可读取的实时页面显示约束，不包含管理员平台配置。 */
export interface DisplaySettings {
  liveLogBufferMiB: number;
}

/** 管理员维护的共享命令；普通用户接口只会返回启用项。 */
export interface CommonCommand {
  id: string;
  command: string;
  description: string;
  enabled: boolean;
  version: number;
  createdAt: string;
  updatedAt: string;
}

export interface CommonCommandBatchItem { id: string; version: number; }
export type CommonCommandBatchAction = "ENABLE" | "DISABLE" | "DELETE";
export interface CommonCommandBatchResult { action: CommonCommandBatchAction; affected: number; }

/** 节点采集性能策略；AUTO 的数值是节点自动调节时可使用的资源上限。 */
export interface PerformancePolicy {
  mode: "AUTO" | "MANUAL";
  bufferMiB: number;
  writeConcurrency: number;
  batchKiB: number;
  flushIntervalMs: number;
  readMiBPerSecond: number;
}

/** Worker 基于新鲜遥测给出的只读推荐；应用前始终由管理员确认。 */
export interface PerformanceRecommendation {
  available: boolean;
  reason: string;
  policy: PerformancePolicy | null;
  resources: {
    effectiveCpu: number;
    memoryLimitBytes: number;
    memoryAvailableBytes: number;
    source: string;
  } | null;
  reportedAt: string | null;
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
  performanceRecommendation?: PerformanceRecommendation | null;
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
  updatePlatform: (body: Pick<PlatformSettings, "retentionDays" | "version"> & { clusterCapacity?: number; liveLogBufferMiB?: number; serviceLogRetentionDays?: number; resourceMonitor?: ResourceMonitorConfig; recordRetention?: RecordRetentionConfig }) =>
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
  commonCommands: (page = 1, pageSize = 100) => request<{ items: CommonCommand[]; total: number; page: number; pageSize: number }>(`/common-commands?page=${page}&pageSize=${pageSize}`),
  adminCommonCommands: (page = 1, pageSize = 20, filters: { query?: string; enabled?: boolean } = {}) => {
    const query = new URLSearchParams({ page: String(page), pageSize: String(pageSize) });
    if (filters.query?.trim()) query.set("query", filters.query.trim());
    if (filters.enabled !== undefined) query.set("enabled", String(filters.enabled));
    return request<{ items: CommonCommand[]; total: number; page: number; pageSize: number }>(`/admin/common-commands?${query}`);
  },
  createCommonCommand: (body: Pick<CommonCommand, "command" | "description" | "enabled">) =>
    request<CommonCommand>("/admin/common-commands", {
      method: "POST", headers: { "Idempotency-Key": idempotencyKey() }, body: JSON.stringify(body),
    }),
  updateCommonCommand: (id: string, body: Partial<Pick<CommonCommand, "command" | "description" | "enabled">> & Pick<CommonCommand, "version">) =>
    request<CommonCommand>(`/admin/common-commands/${encodeURIComponent(id)}`, {
      method: "PATCH", headers: { "Idempotency-Key": idempotencyKey() }, body: JSON.stringify(body),
    }),
  deleteCommonCommand: (id: string, version: number) =>
    request<void>(`/admin/common-commands/${encodeURIComponent(id)}?version=${version}`, { method: "DELETE" }),
  batchCommonCommands: (action: CommonCommandBatchAction, items: CommonCommandBatchItem[], key = idempotencyKey()) =>
    request<CommonCommandBatchResult>("/admin/common-commands/batch", {
      method: "POST", headers: { "Idempotency-Key": key }, body: JSON.stringify({ action, items }),
    }),
};
