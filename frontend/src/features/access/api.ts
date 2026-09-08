// 服务账号接口保持在功能目录内，复用共享请求边界以统一认证和错误处理。
import { idempotencyKey, request } from "../../shared/api";

export interface ServiceToken {
  id: string;
  name: string;
  scopes: string[];
  taskIds: string[] | null;
  expiresAt: string;
  createdAt: string;
  revoked: boolean;
}

export interface ServiceTokenPage {
  items: ServiceToken[];
  total: number;
  page: number;
  pageSize: number;
}

export interface ServiceTokenCreate {
  name: string;
  scopes: string[];
  taskIds?: string[];
  expiresInDays: number;
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
  revoke: (id: string) => request<void>(`/service-tokens/${id}`, { method: "DELETE" }),
};
