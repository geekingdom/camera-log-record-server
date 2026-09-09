// 审计工作区保留独立类型与查询参数，通过统一请求入口复用 Cookie/Token 认证及失效处理。
import { request } from "../../shared/api";
export interface AuditEvent {
  actor?: string;
  action?: string;
  targetId?: string;
  createdAt?: string;
  [key: string]: unknown;
}

export interface RuntimeEvent {
  taskId?: string;
  nodeId?: string;
  type?: string;
  createdAt?: string;
  detectedAt?: string;
  [key: string]: unknown;
}

export interface EventPage<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
}

export interface QueryFilters {
  action?: string;
  actor?: string;
  taskId?: string;
  nodeId?: string;
  type?: string;
  start?: string;
  end?: string;
}

function query(page: number, pageSize: number, filters: QueryFilters) {
  const params = new URLSearchParams({ page: String(page), pageSize: String(pageSize) });
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
  return params;
}

function list<T>(path: string, page: number, pageSize: number, filters: QueryFilters) {
  return request<EventPage<T>>(`${path}?${query(page, pageSize, filters)}`);
}

export const auditApi = {
  auditEvents: (page: number, pageSize: number, filters: QueryFilters) =>
    list<AuditEvent>("/audit-events", page, pageSize, filters),
  runtimeEvents: (page: number, pageSize: number, filters: QueryFilters) =>
    list<RuntimeEvent>("/runtime-events", page, pageSize, filters),
};
