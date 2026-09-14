import { describe, expect, it } from "vitest";
import { normalizeSshTarget, sshTargetLabel } from "./sshTarget";

describe("SSH 主从采集目标", () => {
  it("为 SSH 保留已选从机并展示明确标签", () => {
    expect(normalizeSshTarget("SSH", "SLAVE_2")).toBe("SLAVE_2");
    expect(sshTargetLabel("SLAVE_2")).toBe("从机 2");
  });

  it("协议切换到非 SSH 时复位为主机", () => {
    expect(normalizeSshTarget("TELNET_DEVICE", "SLAVE_1")).toBe("HOST");
    expect(sshTargetLabel(undefined)).toBe("主机");
  });
});
