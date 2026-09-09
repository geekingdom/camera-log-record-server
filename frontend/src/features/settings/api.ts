// 平台设置接口保持在功能目录，复用共享请求边界的认证和错误解析。
import { idempotencyKey, request } from "../../shared/api";

export interface PlatformSettings {
  retentionDays: number;
  version: number;
  updatedAt: string;
}

export interface NodeConfig {
  id: string;
  url: string;
  capacity: number;
  accepting: boolean;
  version: number;
  registered: boolean;
  online: boolean;
  reportedAt: string | null;
  reportedUrl?: string;
  urlMismatch?: boolean;
  activeTasks?: number;
  diskPercent?: number | null;
}

export interface NodeRegistration {
  id: string;
  url: string;
  capacity: number;
  accepting: boolean;
}

export const settingsApi = {
  platform: () => request<PlatformSettings>("/platform-settings"),
  updatePlatform: (body: Pick<PlatformSettings, "retentionDays" | "version">) =>
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
  updateNode: (id: string, body: Pick<NodeConfig, "version" | "capacity" | "accepting">) =>
    request<NodeConfig>(`/admin/nodes/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(body),
    }),
};
