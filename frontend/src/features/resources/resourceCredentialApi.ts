/** 资源密码只通过主动、不可缓存的专用请求读取，不能并入通用资源 API。 */
import { getToken } from "../../shared/api";

export interface ResourceCredential {
  username: string;
  password: string;
}

export async function revealResourceCredential(resourceId: string): Promise<ResourceCredential> {
  const response = await fetch(`/api/v1/resources/${encodeURIComponent(resourceId)}/credential`, {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "读取设备登录凭据失败");
  }
  return response.json() as Promise<ResourceCredential>;
}
