// 普通HTTP内网地址没有randomUUID，写操作仍需生成可靠的幂等键。
import { afterEach, describe, expect, it, vi } from "vitest";
import { idempotencyKey, api } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("HTTP环境幂等键", () => {
  it("缺少randomUUID时使用getRandomValues生成UUIDv4", () => {
    vi.stubGlobal("crypto", { getRandomValues: (bytes: Uint8Array) => bytes.fill(255) });
    expect(idempotencyKey()).toBe("ffffffff-ffff-4fff-bfff-ffffffffffff");
  });
  it("安全上下文继续使用原生UUID", () => {
    vi.stubGlobal("crypto", { randomUUID: () => "native-uuid" });
    expect(idempotencyKey()).toBe("native-uuid");
  });
  it("普通HTTP写操作能发出请求并带幂等键", async () => {
    vi.stubGlobal("crypto", { getRandomValues: (bytes: Uint8Array) => bytes.fill(17) });
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    await api.operation("task-1", "start");
    expect(fetch).toHaveBeenCalledOnce();
    expect(new Headers(fetch.mock.calls[0][1].headers).get("Idempotency-Key"))
      .toBe("11111111-1111-4111-9111-111111111111");
  });
  it("coredump 列表按资源筛选，批量导出携带幂等键", async () => {
    vi.stubGlobal("crypto", { randomUUID: () => "coredump-key" });
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response('{"items":[],"total":0,"page":2,"pageSize":50}', { status: 200 }))
      .mockResolvedValueOnce(new Response('{"id":"export-1","status":"QUEUED"}', { status: 202 }));
    vi.stubGlobal("fetch", fetch);

    await api.coredumps("resource / 1", 2, 50, { name: "core dump", receivedFrom: "2026-09-10T00:00:00+08:00" });
    await api.createCoredumpExport(["file-1", "file-2"]);

    expect(fetch.mock.calls[0][0]).toContain("/resources/resource%20%2F%201/coredumps?page=2&pageSize=50");
    expect(fetch.mock.calls[0][0]).toContain("name=core+dump");
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ fileIds: ["file-1", "file-2"] });
    expect(new Headers(fetch.mock.calls[1][1].headers).get("Idempotency-Key")).toBe("coredump-key");
  });
  it("coredump 下载票据交给同源浏览器导航，不读取完整二进制响应", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response('{"url":"/api/v1/coredumps/file-1/content"}', { status: 200 }));
    vi.stubGlobal("fetch", fetch);

    await api.coredumpBrowserDownload("file-1");

    expect(fetch.mock.calls[0][0]).toBe("/api/v1/coredumps/file-1/browser-session");
    expect(fetch.mock.calls[0][1].method).toBe("POST");
  });
});
