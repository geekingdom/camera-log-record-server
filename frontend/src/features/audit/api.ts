// 审计查询客户端只组装受后端支持的筛选条件，认证和会话失效统一交给共享请求层。
import { request } from "../../shared/api";

export type EventLevel = "INFO" | "WARNING" | "ERROR";
export type EventOutcome = "SUCCEEDED" | "FAILED" | "PENDING" | "CANCELLED" | "UNKNOWN";

export interface EventBase {
  createdAt?: string;
  detectedAt?: string;
  summary?: string;
  level?: EventLevel;
  outcome?: EventOutcome;
  requestId?: string;
  clientIp?: string;
  reason?: string;
  taskId?: string;
  runId?: string;
  sessionId?: string;
  nodeId?: string;
  taskName?: string;
  deviceIp?: string;
  method?: string;
  route?: string;
  httpStatus?: number;
  status?: number | string;
  durationMs?: number;
  [key: string]: unknown;
}

export interface AuditEvent extends EventBase {
  actor?: string;
  actorName?: string;
  action?: string;
  targetId?: string;
  targetName?: string;
}

export interface RuntimeEvent extends EventBase {
  type?: string;
}

export interface RequestEvent extends EventBase {
  actor?: string;
  actorName?: string;
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
  level?: EventLevel;
  outcome?: EventOutcome;
  requestId?: string;
  clientIp?: string;
  method?: string;
  route?: string;
  status?: string;
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
  requestEvents: (page: number, pageSize: number, filters: QueryFilters) =>
    list<RequestEvent>("/request-events", page, pageSize, filters),
};
