// 模板共享配置属于模板写模型；创建者身份来自服务端，客户端不得回写。
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("模板共享写入", () => {
  it("保存共享收件人和全局共享标记，但不回写创建者字段", async () => {
    vi.stubGlobal("crypto", { randomUUID: () => "request-key" });
    vi.stubGlobal("sessionStorage", { getItem: () => null });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "template-1" })));
    vi.stubGlobal("fetch", fetch);

    await api.updateTemplate("template-1", {
      version: 2,
      name: "巡检",
      initialCommands: [],
      scheduledCommands: [],
      sharedWith: ["user-2", "user-3"],
      sharedWithAll: false,
      createdBy: "owner-1",
      createdByName: "创建者",
    });

    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      version: 2,
      name: "巡检",
      initialCommands: [],
      scheduledCommands: [],
      sharedWith: ["user-2", "user-3"],
      sharedWithAll: false,
    });
  });
});
