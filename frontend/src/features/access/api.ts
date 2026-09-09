// 服务账号接口保持在功能目录内，复用共享请求边界以统一认证和错误处理。
import { idempotencyKey, request } from "../../shared/api";

export interface ServiceToken {
  id: string;
  name: string;
  userId: string;
  user?: Pick<import("../../shared/api").SessionUser, "id" | "username" | "displayName" | "enabled" | "deletedAt">;
  expiresAt: string | null;
  createdAt: string;
  version: number;
  revoked: boolean;
  effectiveStatus: "ACTIVE" | "REVOKED" | "EXPIRED" | "USER_DISABLED" | "USER_DELETED" | "USER_MISSING";
}

export interface ServiceTokenPage {
  items: ServiceToken[];
  total: number;
  page: number;
  pageSize: number;
}

export interface ServiceTokenCreate {
  name: string;
  userId: string;
  expiresInDays?: number | null;
}

export interface ServiceTokenUpdate {
  version: number;
  name?: string;
  userId?: string;
  expiresInDays?: number | null;
}

export const accessApi = {
  list: (page = 1, pageSize = 20) =>
    request<ServiceTokenPage>(`/service-tokens?page=${page}&pageSize=${pageSize}`),
  create: (body: ServiceTokenCreate) =>
    request<ServiceToken & { token: string }>("/service-tokens", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(body),
    }),
  update: (id: string, body: ServiceTokenUpdate) => request<ServiceToken>(`/service-tokens/${encodeURIComponent(id)}`, {
    method: "PATCH", body: JSON.stringify(body),
  }),
  reveal: (id: string) => request<{ token: string }>(`/service-tokens/${encodeURIComponent(id)}/reveal`, { method: "POST" }),
  rotate: (id: string, version: number) => request<ServiceToken & { token: string }>(`/service-tokens/${encodeURIComponent(id)}/rotate`, {
    method: "POST", body: JSON.stringify({ version }),
  }),
  revoke: (id: string) => request<void>(`/service-tokens/${id}`, { method: "DELETE" }),
};
