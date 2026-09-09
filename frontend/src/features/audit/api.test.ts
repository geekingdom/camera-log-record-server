// 审计 API 必须复用平台认证：Cookie 登录不能被空 Bearer 覆盖，过期会话应走统一失效流程。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { auditApi } from "./api";

const fetchMock = vi.fn();
let token = "";
beforeEach(() => {
  token = "";
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("sessionStorage", { getItem: () => token });
  vi.stubGlobal("window", new EventTarget());
});
afterEach(() => vi.unstubAllGlobals());

describe("审计工作区认证", () => {
  it.each(["auditEvents", "runtimeEvents"] as const)("%s 保留 Cookie 且不发送空 Bearer", async method => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0, page: 1, pageSize: 50 })));
    await auditApi[method](1, 50, { actor: "管理员 & 运维" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(new Headers(init.headers).has("Authorization")).toBe(false);
    expect(init.credentials).toBe("same-origin");
    expect(new URL(url, "http://localhost").searchParams.get("actor")).toBe("管理员 & 运维");
  });

  it("有效服务 Token 仍按原值发送", async () => {
    token = "service-test-token";
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0 })));
    await auditApi.auditEvents(1, 50, {});
    expect(new Headers(fetchMock.mock.calls[0][1].headers).get("Authorization")).toBe("Bearer service-test-token");
  });

  it("真实会话失效触发统一退出并保留请求编号", async () => {
    const expired = vi.fn();
    window.addEventListener("auth-required", expired);
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ error: { message: "登录会话已失效，请重新登录" } }), {
      status: 401, headers: { "X-Auth-Required": "true", "X-Request-ID": "audit-request" },
    }));
    await expect(auditApi.auditEvents(1, 50, {})).rejects.toMatchObject({ status: 401, requestId: "audit-request" });
    expect(expired).toHaveBeenCalledOnce();
  });

  it("无管理员权限只返回403，不伪装成登录过期", async () => {
    const expired = vi.fn();
    window.addEventListener("auth-required", expired);
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ error: { message: "权限不足" } }), { status: 403 }));
    await expect(auditApi.runtimeEvents(1, 50, {})).rejects.toMatchObject({ status: 403 });
    expect(expired).not.toHaveBeenCalled();
  });
});
