// 内置文档只读取服务端目录；它必须沿用平台的 Cookie 与服务 Token 认证边界。
import { afterEach, describe, expect, it, vi } from "vitest";
import { describeObjectFields, describeSchema, loadApiReference, type ReferenceSchema } from "./apiReference";

afterEach(() => vi.unstubAllGlobals());

describe("内置接口目录", () => {
  it("通过既有请求边界读取 catalog，并保留当前会话", async () => {
    vi.stubGlobal("sessionStorage", { getItem: () => "reference-token" });
    const pythonExample = "# pip install requests\nrequests.get('http://<服务地址>/api/v1/resources')";
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      version: "1.0", guides: [], operations: [{
        id: "list-resources", method: "GET", path: "/resources", title: "资源列表", group: "资源", description: "读取资源。",
        permission: "resources:read", headers: {}, parameters: [], requestExample: null,
        pythonExample, responseStatus: 200, responseExample: { items: [] },
        responseContract: { kind: "json", description: "JSON", schema: { type: "object", properties: {} }, errors: [], errorSchema: null },
      }], schemas: {}, errorExample: {},
    })));
    vi.stubGlobal("fetch", fetch);

    await expect(loadApiReference()).resolves.toMatchObject({
      version: "1.0", operations: [expect.objectContaining({ pythonExample })],
    });
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

it("返回字段完整展开可空字典和所有联合分支，递归引用不无限展开", () => {
  const schema: ReferenceSchema = { type: "object", properties: {
    values: { type: "object", nullable: true, description: "动态指标", additionalProperties: {
      type: "object", description: "指标值", properties: { value: { type: "number", description: "读数" } },
    } },
    result: { oneOf: [
      { type: "object", properties: { data: { type: "string", description: "正文" } } },
      { type: "object", properties: { reason: { type: "string", description: "缺口原因" } } },
    ] },
  } };
  const fields = describeObjectFields(schema, {});
  expect(fields.map(field => field.path)).toEqual([
    "values", "values.{键}", "values.{键}.value", "result", "result.{分支1}.data", "result.{分支2}.reason",
  ]);
  expect(fields[0].type).toBe("object | null");
  expect(describeObjectFields({ $ref: "#/Node" }, { Node: { type: "object", properties: {
    child: { $ref: "#/Node", description: "子节点" },
  } } }).map(field => field.path)).toEqual(["child"]);
});
