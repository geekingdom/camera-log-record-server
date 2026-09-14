// 统一 API 边界：负责认证、幂等键、字段白名单和可关联服务端日志的错误信息。
import type {
  LogFile,
  CommandExecution,
  CoredumpExport,
  CoredumpFile,
  CoredumpMonitorStatus,
  CommandExecutionPage,
  InitialCommand,
  LogHour,
  Node,
  Page,
  Resource,
  ResourceAuthentication,
  AuthenticationRecord,
  AuthenticationRecordResult,
  CursorPage,
  ResourceAuthType,
  ResourceKind,
  ResourceMetricsPage,
  Task,
  Template,
} from "./types";

const base = "/api/v1";
const tokenKey = "camera-log-record-token";
export const getToken = () => sessionStorage.getItem(tokenKey) ?? "";
export const setToken = (token: string) =>
  sessionStorage.setItem(tokenKey, token.trim());
export const clearToken = () => sessionStorage.removeItem(tokenKey);
export const idempotencyKey = (): string => {
  if (typeof globalThis.crypto.randomUUID === "function") return globalThis.crypto.randomUUID();
  // 内网HTTP不提供randomUUID，但getRandomValues可用；保留UUIDv4随机性及格式。
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
};
export interface SessionUser { id: string; username: string; displayName: string; isAdmin: boolean; scopes: string[]; enabled: boolean; mustChangePassword: boolean; version?: number; builtin?: boolean; deletedAt?: string | null; }
export interface UserPage { items: SessionUser[]; total: number; page: number; pageSize: number; }
export interface IpPolicy { version: number; enabled: boolean; clientIp: string; rules: { label: string; network: string; scopes: string[] }[]; }

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public requestId?: string,
    public code?: string,
  ) {
    super(requestId ? `${message}（请求 ID: ${requestId}）` : message);
  }
}
// 所有 REST 请求经过此处，避免各功能模块遗漏 Bearer Token 或请求 ID 错误提示。
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  if (init.method && init.method !== "GET" && init.method !== "HEAD") headers.set("X-Requested-With", "XMLHttpRequest");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${base}${path}`, { ...init, headers, credentials: "same-origin" });
  if (!response.ok) {
    if (response.status === 401 && response.headers.get("x-auth-required") === "true") window.dispatchEvent(new Event("auth-required"));
    let message = response.statusText;
    let code: string | undefined;
    try {
      const body = await response.json();
      message =
        body.message ??
        body.detail?.message ??
        body.error?.message ??
        body.error ??
        JSON.stringify(body);
      code = typeof body.code === "string" ? body.code
        : typeof body.detail?.code === "string" ? body.detail.code
          : typeof body.error?.code === "string" ? body.error.code : undefined;
    } catch {
      /* text response */
    }
    throw new ApiError(
      response.status,
      message || `请求失败 (${response.status})`,
      response.headers.get("x-request-id") ??
      response.headers.get("request-id") ??
        undefined,
      code,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export const authApi = {
  login: async (username: string, password: string) => { clearToken(); return request<{ user: SessionUser }>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }); },
  me: () => request<{ user: SessionUser }>("/auth/me"),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  password: (currentPassword: string, newPassword: string) => request<{ user: SessionUser }>("/auth/password", { method: "POST", body: JSON.stringify({ currentPassword, newPassword }) }),
};
export const usersApi = {
  list: (page = 1, pageSize = 20) => request<UserPage>(`/users?page=${page}&pageSize=${pageSize}`),
  permissions: () => request<{ scopes: { value: string; label: string }[] }>("/users/permissions"),
  shareTargets: (page = 1, pageSize = 100) => request<Page<Pick<SessionUser, "id" | "username" | "displayName">>>(`/users/share-targets${query(page, pageSize)}`),
  create: (body: Pick<SessionUser, "username" | "displayName" | "isAdmin" | "scopes" | "enabled"> & { password: string }) => request<SessionUser>("/users", { method: "POST", body: JSON.stringify(body) }),
  update: (id: string, body: Partial<Pick<SessionUser, "displayName" | "scopes" | "enabled">> & { version: number }) => request<SessionUser>(`/users/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) }),
  resetPassword: (id: string, password: string, version: number) => request<void>(`/users/${encodeURIComponent(id)}/reset-password`, { method: "POST", body: JSON.stringify({ password, version }) }),
  disable: (id: string, version: number) => request<void>(`/users/${encodeURIComponent(id)}?version=${version}`, { method: "DELETE" }),
};
export const ipPolicyApi = { get: () => request<IpPolicy>("/admin/ip-policy"), update: (body: IpPolicy) => request<IpPolicy>("/admin/ip-policy", { method: "PATCH", body: JSON.stringify(body) }) };
const query = (
  page = 1,
  pageSize = 20,
  filters: Record<string, string | undefined> = {},
) => {
  const params = new URLSearchParams({
    page: String(page),
    pageSize: String(pageSize),
  });
  Object.entries(filters).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  return `?${params}`;
};
// 写入任务时仅发送后端允许的配置字段，防止列表/详情中的运行时字段被回写。
const taskFields = [
  "name",
  "description",
  "protocol",
  "sshTarget",
  "ip",
  "port",
  "username",
  "password",
  "initialCommands",
  "scheduledCommands",
  "sourceTemplateId",
  "sourceTemplateVersion",
  "encoding",
  "loginPrompt",
  "passwordPrompt",
  "resourceId",
  "serialServerResourceId",
  "clearPassword",
];
const templateFields = [
  "name",
  "description",
  "initialCommands",
  "scheduledCommands",
  "sharedWith",
  "sharedWithAll",
];
function pick(source: object, fields: string[]) {
  return Object.fromEntries(
    Object.entries(source).filter(
      ([key, value]) => fields.includes(key) && value !== undefined,
    ),
  );
}
export const api = {
  resources: (
    page?: number,
    pageSize?: number,
    filters?: { search?: string; model?: string; subSerialNumber?: string; kind?: ResourceKind; includeDeleted?: string; createdBy?: string },
  ) => request<Page<Resource>>(`/resources${query(page, pageSize, filters)}`),
  resource: (id: string) => request<Resource>(`/resources/${id}`),
  /** 监控历史按资源读取；仅拼接已定义参数，避免把 undefined 送入服务端。 */
  resourceMetrics: (resourceId: string, filters: {
    start?: string;
    end?: string;
    limit?: number;
    cursor?: string;
  } = {}) => {
    const params = new URLSearchParams();
    if (filters.start !== undefined) params.set("start", filters.start);
    if (filters.end !== undefined) params.set("end", filters.end);
    if (filters.limit !== undefined) params.set("limit", String(filters.limit));
    if (filters.cursor !== undefined) params.set("cursor", filters.cursor);
    return request<ResourceMetricsPage>(
      `/resources/${encodeURIComponent(resourceId)}/resource-metrics?${params}`,
    );
  },
  updateResource: (id: string, resource: object) => request<Resource>(`/resources/${id}`, {
    method: "PATCH", body: JSON.stringify(resource),
  }),
  deleteResource: (id: string, version: number) => request<Resource>(`/resources/${id}?version=${version}`, {
    method: "DELETE",
  }),
  authenticateResource: (input: {
    name: string;
    kind: ResourceKind;
    ip: string;
    username: string;
    password: string;
    authType: ResourceAuthType;
  }, resourceId?: string) =>
    request<ResourceAuthentication>(resourceId ? `/resources/${resourceId}/authenticate` : "/resources/authenticate", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(input),
    }),
  /** 资源列表只触发服务端使用已保存密文凭据的认证，绝不将密码读回浏览器。 */
  authenticateSavedResource: (resourceId: string) => request<ResourceAuthentication>(
    `/resources/${encodeURIComponent(resourceId)}/authenticate`, { method: "POST" },
  ),
  authenticationRecords: (resourceId: string, page = 1, filters?: {
    result?: AuthenticationRecordResult;
    start?: string;
    end?: string;
    identityChanged?: "true" | "false";
  }) => request<Page<AuthenticationRecord>>(
    `/resources/${encodeURIComponent(resourceId)}/authentication-records${query(page, 20, filters)}`,
  ),
  // 游标首屏必须保留 `cursor=`，不能复用会忽略空字符串的通用 query()。
  authenticationRecordsCursor: (resourceId: string, cursor: string, filters?: {
    result?: AuthenticationRecordResult;
    start?: string;
    end?: string;
    identityChanged?: "true" | "false";
  }) => {
    const params = new URLSearchParams({ pageSize: "20", cursor });
    Object.entries(filters ?? {}).forEach(([key, value]) => {
      if (value !== undefined) params.set(key, value);
    });
    return request<CursorPage<AuthenticationRecord>>(
      `/resources/${encodeURIComponent(resourceId)}/authentication-records?${params}`,
    );
  },
  coredumps: (resourceId: string, page = 1, pageSize = 50, filters?: {
    name?: string; receivedFrom?: string; receivedTo?: string;
  }) => request<Page<CoredumpFile>>(`/resources/${encodeURIComponent(resourceId)}/coredumps${query(page, pageSize, filters)}`),
  coredumpMonitor: (resourceId: string) => request<CoredumpMonitorStatus>(
    `/resources/${encodeURIComponent(resourceId)}/coredump-monitor`,
  ),
  createCoredumpExport: (fileIds: string[]) => request<CoredumpExport>("/coredump-exports", {
    method: "POST", headers: { "Idempotency-Key": idempotencyKey() }, body: JSON.stringify({ fileIds }),
  }),
  coredumpExport: (id: string) => request<CoredumpExport>(`/coredump-exports/${encodeURIComponent(id)}`),
  cancelCoredumpExport: (id: string) => request<void>(`/coredump-exports/${encodeURIComponent(id)}`, { method: "DELETE" }),
  coredumpBrowserDownload: (id: string, exportFile = false) => {
    const prefix = exportFile ? "/coredump-exports" : "/coredumps";
    return request<{ url: string }>(`${prefix}/${encodeURIComponent(id)}/browser-session`, { method: "POST" });
  },
  createResource: (resource: {
    name: string;
    kind: ResourceKind;
    ip: string;
    username?: string;
    password?: string;
    authType?: ResourceAuthType;
    enableCoredumpMonitor?: boolean;
    enableResourceMonitor?: boolean;
  }) =>
    request<Resource>("/resources", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(resource),
    }),
  // 列表筛选通过后端分页完成，避免只搜索当前页造成“找不到”错觉。
  tasks: (
    page?: number,
    pageSize?: number,
    filters?: { search?: string; status?: string; resourceId?: string; createdBy?: string },
  ) => request<Page<Task>>(`/tasks${query(page, pageSize, filters)}`),
  task: (id: string) => request<Task>(`/tasks/${id}`),
  createTask: (
    task: Omit<Task, "id" | "version" | "status" | "desiredState">,
    autoStart: boolean,
  ) =>
    request<Task>(`/tasks`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify({ ...pick(task, taskFields), autoStart }),
    }),
  updateTask: (id: string, task: Partial<Task> & { version: number }) =>
    request<Task>(`/tasks/${id}`, {
      method: "PATCH",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(pick(task, [...taskFields, "version"])),
    }),
  operation: (id: string, action: "start" | "stop" | "pause" | "resume") =>
    request<unknown>(`/tasks/${id}/${action}`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
    }),
  restartBlocked: (id: string, body: { confirmIsolation?: boolean; evidence?: string } = {}) =>
    request<unknown>(`/tasks/${id}/restart`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(body),
    }),
  operationStatus: (id: string) => request<{ status?: string; phase?: string }>(`/operations/${id}`),
  logHours: (id: string, date?: string, page = 1) => request<Page<LogHour>>(`/tasks/${id}/log-hours${query(page, 24, { date })}`),
  // 缺口阅读器关闭或切换任务时取消读取，避免失效查询继续占用连接。
  fileContent: (id: string, offset = 0, limit = 65536, signal?: AbortSignal) => request<{ fileId: string; sessionId?: string; data: string; nextOffset: number }>(`/log-files/${id}/content?offset=${offset}&limit=${limit}`, { signal }),
  logFile: (id: string) => request<LogFile>(`/log-files/${encodeURIComponent(id)}`),
  command: (id: string, command: InitialCommand) =>
    request<CommandExecution>(`/tasks/${id}/commands`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(command),
    }),
  executions: (id: string, page = 1, filters?: { commandId?: string; kind?: "MANUAL" | "SCHEDULED" }) =>
    request<CommandExecutionPage>(`/tasks/${id}/command-executions${query(page, 50, filters)}`),
  templates: (page?: number, pageSize?: number, filters: Record<string, string | undefined> = {}) =>
    request<Page<Template>>(`/command-templates${query(page, pageSize, filters)}`),
  template: (id: string) => request<Template>(`/command-templates/${id}`),
  createTemplate: (template: Omit<Template, "id" | "version">) =>
    request<Template>("/command-templates", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(pick(template, templateFields)),
    }),
  updateTemplate: (
    id: string,
    template: Partial<Template> & { version: number },
  ) =>
    request<Template>(`/command-templates/${id}`, {
      method: "PATCH",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(pick(template, [...templateFields, "version"])),
    }),
  deleteTemplate: (id: string, version: number) =>
    request<void>(`/command-templates/${id}?version=${version}`, {
      method: "DELETE",
    }),
  nodes: (page?: number, pageSize?: number) =>
    request<Page<Node>>(`/nodes${query(page, pageSize)}`),
  download: (taskId: string, hourIds: string[], allowPartial: boolean) =>
    request<{ id: string }>("/downloads", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify({ taskId, hourIds, allowPartial }),
    }),
  downloadStatus: (id: string) =>
    request<{
      id: string;
      status: string;
      filename?: string;
      progress?: number;
      error?: string;
    }>(`/downloads/${id}`),
  search: (taskId: string, keyword: string, start?: string, end?: string) =>
    request<{ id: string }>("/log-searches", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify({ taskId, keyword, start, end }),
    }),
  searchResults: (id: string, page = 1) =>
    request<Page<Record<string, unknown>>>(`/log-searches/${id}/results${query(page, 100)}`),
  searchStatus: (id: string) =>
    request<{
      id: string;
      status: string;
      progress?: number;
      error?: string;
      truncated?: boolean;
    }>(`/log-searches/${id}`),
  cancelJob: (id: string, kind: "downloads" | "log-searches") =>
    request<void>(`/${kind}/${id}`, { method: "DELETE" }),
  browserDownload: (id: string) =>
    request<{ url: string }>(`/downloads/${id}/browser-session`, {
      method: "POST",
    }),
};
export async function fetchDownload(id: string) {
  const response = await fetch(`${base}/downloads/${id}/content`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!response.ok)
    throw new ApiError(response.status, `下载失败 (${response.status})`);
  return response.blob();
}
