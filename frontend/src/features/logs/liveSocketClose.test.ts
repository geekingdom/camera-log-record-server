import { describe, expect, it } from "vitest";

import { shouldReconnectLiveSocket } from "./liveSocketClose";

describe("shouldReconnectLiveSocket", () => {
  it("服务端以正常关闭结束已停止或已删除任务时不重连", () => {
    expect(shouldReconnectLiveSocket(1000)).toBe(false);
  });

  it("认证、授权和节点异常关闭仍然重连", () => {
    expect(shouldReconnectLiveSocket(4401)).toBe(true);
    expect(shouldReconnectLiveSocket(4403)).toBe(true);
    expect(shouldReconnectLiveSocket(1013)).toBe(true);
  });

  it("本地传输错误触发的默认 1000 关闭仍然重连", () => {
    expect(shouldReconnectLiveSocket(1000, true)).toBe(true);
  });
});
