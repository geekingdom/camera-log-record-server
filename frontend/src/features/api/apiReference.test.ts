// 内置文档只读取服务端目录；它必须沿用平台的 Cookie 与服务 Token 认证边界。
import { afterEach, describe, expect, it, vi } from "vitest";
import { describeObjectFields, describeSchema, loadApiReference } from "./apiReference";

afterEach(() => vi.unstubAllGlobals());

describe("内置接口目录", () => {
  it("通过既有请求边界读取 catalog，并保留当前会话", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => "reference-token" });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      version: "1.0", guides: [], operations: [], schemas: {}, errorExample: {},
    })));
    vi.stubGlobal("fetch", fetch);

    await expect(loadApiReference()).resolves.toMatchObject({ version: "1.0" });
    expect(fetch).toHaveBeenCalledOnce();
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/api/v1/api-reference");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer reference-token");
    expect(init.credentials).toBe("same-origin");
  });

  it("展开引用、可空联合和数组元素类型，保留字段约束", () => {
    const schemas = {
      Mode: { type: "string", enum: ["SSH", "TELNET"], default: "SSH" },
      OptionalMode: { anyOf: [{ $ref: "#/components/schemas/Mode" }, { type: "null" }] },
    };
    expect(describeSchema({ $ref: "#/components/schemas/OptionalMode" }, schemas, true)).toEqual({
      type: "string | null", constraints: "必填 · 可选 SSH / TELNET · 默认 \"SSH\"",
    });
    expect(describeSchema({ type: "array", items: { type: "integer", minimum: 1 }, maxItems: 3 }, schemas, false)).toEqual({
      type: "array<integer>", constraints: "可选 · 最多 3 项",
    });
  });

  it("递归列出嵌套对象与数组元素的正式字段说明", () => {
    const schemas = {
      Command: { type: "object", required: ["command"], properties: {
        command: { type: "string", description: "发送给设备的一条单行命令正文。" },
      } },
    };
    expect(describeObjectFields({ type: "object", properties: {
      initialCommands: { type: "array", description: "初始化命令列表。", items: { $ref: "#/components/schemas/Command" } },
    } }, schemas)).toEqual([
      expect.objectContaining({ path: "initialCommands", description: "初始化命令列表。" }),
      expect.objectContaining({ path: "initialCommands[].command", description: "发送给设备的一条单行命令正文。", required: true }),
    ]);
  });
});
