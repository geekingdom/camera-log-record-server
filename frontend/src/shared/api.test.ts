// API 边界必须保留服务端的隔离错误码，并按专用路径提交管理员确认。
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("设备身份模糊筛选 API", () => {
  it("独立编码型号、序列号和 coredump 存在状态，并保留已有组合条件", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0 })));
    vi.stubGlobal("fetch", fetch);
    await api.resources(1, 20, {
      model: "G2/VX3+", subSerialNumber: "GA[154]", search: "大厅", createdBy: "operator", includeDeleted: "true", hasCoredumps: "false",
    });
    const params = new URL(fetch.mock.calls[0][0], "http://localhost").searchParams;
    expect(Object.fromEntries(params)).toEqual({
      page: "1", pageSize: "20", model: "G2/VX3+", subSerialNumber: "GA[154]", search: "大厅",
      createdBy: "operator", includeDeleted: "true", hasCoredumps: "false",
    });
  });

  it("保留存在 coredump 的明确筛选值", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0 })));
    vi.stubGlobal("fetch", fetch);

    await api.resources(1, 20, { hasCoredumps: "true" });

    expect(new URL(fetch.mock.calls[0][0], "http://localhost").searchParams.get("hasCoredumps")).toBe("true");
  });

  it("仅看自己由专用 mine 参数请求，且可独立包含已删除资源", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0 })));
    vi.stubGlobal("fetch", fetch);

    await api.resources(2, 20, { mine: "true", includeDeleted: "true" });

    expect(new URL(fetch.mock.calls[0][0], "http://localhost").searchParams).toEqual(
      new URLSearchParams({ page: "2", pageSize: "20", mine: "true", includeDeleted: "true" }),
    );
  });
});

describe("资源共享 API", () => {
  it("创建和编辑资源时保留显式共享用户列表", async () => {
    vi.stubGlobal("crypto", { randomUUID: () => "request-key" });
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "resource-1" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "resource-1" })));
    vi.stubGlobal("fetch", fetch);

    await api.createResource({ name: "共享资源", kind: "SERIAL_SERVER", ip: "192.0.2.1", sharedUserIds: ["user-a", "user-b"] });
    await api.updateResource("resource-1", { version: 3, sharedUserIds: [] });

    expect(JSON.parse(fetch.mock.calls[0][1].body)).toMatchObject({ sharedUserIds: ["user-a", "user-b"] });
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ version: 3, sharedUserIds: [] });
  });
});

describe("运行看板 API", () => {
  it("只按约定传递统计窗口和上海自然日", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({})));
    vi.stubGlobal("fetch", fetch);

    await api.dashboard(14, "2026-10-14");

    expect(fetch.mock.calls[0][0]).toBe("/api/v1/dashboard?days=14&date=2026-10-14");
  });
});

describe("任务下载历史 API", () => {
  it("按任务读取固定大小的下载记录页，并编码任务标识", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      items: [], total: 0, page: 1, pageSize: 20,
    })));
    vi.stubGlobal("fetch", fetch);

    await api.taskDownloads("task id", 1);

    expect(fetch.mock.calls[0][0]).toBe("/api/v1/tasks/task%20id/downloads?page=1&pageSize=20");
  });
});

describe("检索索引覆盖 API", () => {
  it("按任务与 UTC 半开区间读取覆盖状态", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "DISABLED" })));
    vi.stubGlobal("fetch", fetch);

    await api.searchIndexStatus("task id", "2026-10-16T00:00:00.000Z", "2026-10-16T01:00:00.000Z");

    expect(fetch.mock.calls[0][0]).toBe("/api/v1/tasks/task%20id/search-index-status?start=2026-10-16T00%3A00%3A00.000Z&end=2026-10-16T01%3A00%3A00.000Z");
  });
});

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

describe("批量资源 API", () => {
  it("保留调用方给出的稳定幂等键，并把逐项失败详情暴露给界面", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      message: "存在认证失败设备", details: [{ ip: "10.0.0.2", message: "认证失败" }],
    }), { status: 422 }));
    vi.stubGlobal("fetch", fetch);
    await expect(api.createResourcesBatch([{
      name: "10.0.0.2", kind: "HIKVISION_NETWORK", ip: "10.0.0.2", username: "admin", password: "secret",
      authType: "DIGEST", enableCoredumpMonitor: false, enableResourceMonitor: false,
    }], "stable-batch-key")).rejects.toMatchObject({
      details: [{ ip: "10.0.0.2", message: "认证失败" }],
    });
    expect(new Headers(fetch.mock.calls[0][1].headers).get("Idempotency-Key")).toBe("stable-batch-key");
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
