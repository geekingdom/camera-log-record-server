// API 边界必须保留服务端的隔离错误码，并按专用路径提交管理员确认。
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("阻塞任务重新启动 API", () => {
  it("提交隔离确认并保留服务端 detail 中的错误码", async () => {
    vi.stubGlobal("crypto", { randomUUID: () => "request-key" });
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: "ISOLATION_REQUIRED", message: "需隔离" } }), { status: 409 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "operation-1" })));
    vi.stubGlobal("fetch", fetch);

    await expect(api.restartBlocked("task-1")).rejects.toMatchObject({ status: 409, code: "ISOLATION_REQUIRED" });
    await api.restartBlocked("task-1", { confirmIsolation: true, evidence: "旧实例已由值班人员隔离" });

    expect(fetch.mock.calls[1][0]).toBe("/api/v1/tasks/task-1/restart");
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ confirmIsolation: true, evidence: "旧实例已由值班人员隔离" });
  });
});

describe("认证记录游标 API", () => {
  it("首屏保留空 cursor，避免回退为带总数的页码查询", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      items: [], total: null, pageSize: 20, hasMore: false, nextCursor: null,
    })));
    vi.stubGlobal("fetch", fetch);

    await api.authenticationRecordsCursor("resource id", "", { result: "SUCCESS" });

    expect(fetch.mock.calls[0][0]).toBe(
      "/api/v1/resources/resource%20id/authentication-records?pageSize=20&cursor=&result=SUCCESS",
    );
  });
});

describe("资源列表即时认证 API", () => {
  it("只向已有资源认证路径发送空请求体", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ model: "DS-2CD" }), { status: 200 }));
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    vi.stubGlobal("fetch", fetchMock);

    await api.authenticateSavedResource("resource id");

    expect(fetchMock).toHaveBeenCalledWith("/api/v1/resources/resource%20id/authenticate", expect.objectContaining({
      method: "POST", credentials: "same-origin",
    }));
    expect(fetchMock.mock.calls[0][1].body).toBeUndefined();
  });
});

describe("资源监控历史 API", () => {
  it("编码资源标识并只传递已定义的游标和时间范围", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] })));
    vi.stubGlobal("fetch", fetch);

    await api.resourceMetrics("resource id", {
      start: "2026-09-11T00:00:00.000Z",
      end: "2026-09-11T01:00:00.000Z",
      limit: 2000,
    });

    expect(fetch.mock.calls[0][0]).toBe(
      "/api/v1/resources/resource%20id/resource-metrics?start=2026-09-11T00%3A00%3A00.000Z&end=2026-09-11T01%3A00%3A00.000Z&limit=2000",
    );
  });
});
