// 统一 API 边界：负责认证、幂等键、字段白名单和可关联服务端日志的错误信息。
import type {
  CommandExecution,
  InitialCommand,
  LogHour,
  Node,
  Page,
  Task,
  Template,
} from "./types";

const base = "/api/v1";
const tokenKey = "camera-log-record-token";
export const getToken = () => sessionStorage.getItem(tokenKey) ?? "";
export const setToken = (token: string) =>
  sessionStorage.setItem(tokenKey, token.trim());
export const clearToken = () => sessionStorage.removeItem(tokenKey);
export const idempotencyKey = () => crypto.randomUUID();

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public requestId?: string,
  ) {
    super(requestId ? `${message}（请求 ID: ${requestId}）` : message);
  }
}
// 所有 REST 请求经过此处，避免各功能模块遗漏 Bearer Token 或请求 ID 错误提示。
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${base}${path}`, { ...init, headers });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const body = await response.json();
      message =
        body.message ??
        body.error?.message ??
        body.error ??
        JSON.stringify(body);
    } catch {
      /* text response */
    }
    throw new ApiError(
      response.status,
      message || `请求失败 (${response.status})`,
      response.headers.get("x-request-id") ??
        response.headers.get("request-id") ??
        undefined,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
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
  "clearPassword",
];
const templateFields = [
  "name",
  "description",
  "initialCommands",
  "scheduledCommands",
];
function pick(source: object, fields: string[]) {
  return Object.fromEntries(
    Object.entries(source).filter(
      ([key, value]) => fields.includes(key) && value !== undefined,
    ),
  );
}
export const api = {
  // 列表筛选通过后端分页完成，避免只搜索当前页造成“找不到”错觉。
  tasks: (
    page?: number,
    pageSize?: number,
    filters?: { search?: string; status?: string },
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
  logHours: (id: string) => request<Page<LogHour>>(`/tasks/${id}/log-hours`),
  command: (id: string, command: InitialCommand) =>
    request<CommandExecution>(`/tasks/${id}/commands`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(command),
    }),
  executions: (id: string) =>
    request<Page<CommandExecution>>(`/tasks/${id}/command-executions`),
  templates: (page?: number, pageSize?: number) =>
    request<Page<Template>>(`/command-templates${query(page, pageSize)}`),
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
  searchResults: (id: string) =>
    request<Page<Record<string, unknown>>>(`/log-searches/${id}/results`),
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
