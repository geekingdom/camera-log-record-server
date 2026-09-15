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
  version: number;
  updatedAt: string;
  resourceMonitor?: ResourceMonitorConfig;
  recordRetention?: RecordRetentionConfig;
}

export interface NodeConfig {
  id: string;
  url: string;
  capacity: number;
  accepting: boolean;
  inputRateLimitMiB?: number;
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
}

export interface NodeRegistration {
  id: string;
  url: string;
  capacity: number;
  accepting: boolean;
  inputRateLimitMiB?: number;
  isGeneralNode?: boolean;
  resourceNetworks?: string[];
}

export const settingsApi = {
  platform: () => request<PlatformSettings>("/platform-settings"),
  updatePlatform: (body: Pick<PlatformSettings, "retentionDays" | "version"> & { clusterCapacity?: number; resourceMonitor?: ResourceMonitorConfig; recordRetention?: RecordRetentionConfig }) =>
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
  updateNode: (id: string, body: Pick<NodeConfig, "version" | "capacity" | "accepting" | "isGeneralNode" | "resourceNetworks" | "inputRateLimitMiB">) =>
    request<NodeConfig>(`/admin/nodes/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(body),
    }),
};
