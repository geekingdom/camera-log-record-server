// 审计工作区的局部只读 API 边界；复用控制台会话键但不扩展共享业务客户端。
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

export class AuditApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function query(page: number, pageSize: number, filters: QueryFilters) {
  const params = new URLSearchParams({ page: String(page), pageSize: String(pageSize) });
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
  return params;
}

async function list<T>(path: string, page: number, pageSize: number, filters: QueryFilters) {
  const response = await fetch(`/api/v1${path}?${query(page, pageSize, filters)}`, {
    headers: { Authorization: `Bearer ${sessionStorage.getItem("camera-log-record-token") ?? ""}` },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new AuditApiError(response.status, body.error?.message ?? `请求失败 (${response.status})`);
  }
  return response.json() as Promise<EventPage<T>>;
}

export const auditApi = {
  auditEvents: (page: number, pageSize: number, filters: QueryFilters) =>
    list<AuditEvent>("/audit-events", page, pageSize, filters),
  runtimeEvents: (page: number, pageSize: number, filters: QueryFilters) =>
    list<RuntimeEvent>("/runtime-events", page, pageSize, filters),
};
