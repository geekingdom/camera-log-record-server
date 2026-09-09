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
});
