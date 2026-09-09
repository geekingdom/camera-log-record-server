// 服务令牌仅绑定既有用户；权限和资源范围不能再作为令牌自身配置写入请求体。
import { afterEach, describe, expect, it, vi } from "vitest";
import { accessApi } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("服务令牌合同", () => {
  it("创建请求只提交令牌名称、所属用户和有效期", async () => {
    vi.stubGlobal("crypto", { randomUUID: () => "request-key" });
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ token: "one-time" })));
    vi.stubGlobal("fetch", fetch);

    await accessApi.create({ name: "第三方只读", userId: "user-7", expiresInDays: 30 });

    const body = JSON.parse(fetch.mock.calls[0][1].body);
    expect(body).toEqual({ name: "第三方只读", userId: "user-7", expiresInDays: 30 });
    expect(body).not.toHaveProperty("scopes");
    expect(body).not.toHaveProperty("taskIds");
  });

  it("永久服务令牌在创建时以 null 表示有效期", async () => {
    vi.stubGlobal("crypto", { randomUUID: () => "request-key" });
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ token: "one-time" })));
    vi.stubGlobal("fetch", fetch);

    await accessApi.create({ name: "永久服务", userId: "user-7", expiresInDays: null });

    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ name: "永久服务", userId: "user-7", expiresInDays: null });
  });

  it("编辑请求携带乐观锁版本与新的绑定用户", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "token-1" })));
    vi.stubGlobal("fetch", fetch);

    await accessApi.update("token-1", { version: 4, name: "已改名", userId: "user-8" });

    expect(fetch.mock.calls[0][0]).toBe("/api/v1/service-tokens/token-1");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ version: 4, name: "已改名", userId: "user-8" });
  });

  it("编辑永久服务令牌时以 null 重设有效期", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "token-1" })));
    vi.stubGlobal("fetch", fetch);

    await accessApi.update("token-1", { version: 4, expiresInDays: null });

    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ version: 4, expiresInDays: null });
  });

  it("查看与重新生成分别使用专用口令端点", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockImplementation(() => new Response(JSON.stringify({ token: "new-secret" })));
    vi.stubGlobal("fetch", fetch);

    await accessApi.reveal("token-1");
    await accessApi.rotate("token-1", 4);

    expect(fetch.mock.calls[0][0]).toBe("/api/v1/service-tokens/token-1/reveal");
    expect(fetch.mock.calls[0][1].method).toBe("POST");
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/service-tokens/token-1/rotate");
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ version: 4 });
  });

  it("保留服务端的稳定错误代码以区分旧口令和并发冲突", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { code: "SERVICE_TOKEN_LEGACY_SECRET", message: "旧口令无法恢复" },
    }), { status: 409 })));

    await expect(accessApi.reveal("token-1")).rejects.toMatchObject({
      status: 409,
      code: "SERVICE_TOKEN_LEGACY_SECRET",
    });
  });
});
