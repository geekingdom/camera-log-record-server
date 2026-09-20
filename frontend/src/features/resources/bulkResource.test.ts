import { describe, expect, it, vi } from "vitest";
import { createBulkResourceDraft, expandIpRange } from "./bulkResource";

describe("expandIpRange", () => {
  it("expands inclusive IPv4 and IPv6 ranges without crossing families", () => {
    expect(expandIpRange("192.168.1.254", "192.168.2.1")).toEqual([
      "192.168.1.254", "192.168.1.255", "192.168.2.0", "192.168.2.1",
    ]);
    expect(expandIpRange("2001:db8::fffe", "2001:db8::1:1")).toEqual([
      "2001:db8::fffe", "2001:db8::ffff", "2001:db8::1:0", "2001:db8::1:1",
    ]);
    expect(() => expandIpRange("192.168.1.1", "::1")).toThrow("同一地址族");
    expect(() => expandIpRange("1:2:3:4:5:6:7:8::", "1:2:3:4:5:6:7:8::")).toThrow("IPv6");
    expect(() => expandIpRange("192.0.2.1::", "192.0.2.1::")).toThrow("IPv6");
  });

  it("rejects a range above the 256-resource limit", () => {
    expect(() => expandIpRange("10.0.0.1", "10.0.1.1")).toThrow("最多 256");
  });
});

describe("createBulkResourceDraft", () => {
  it("invalidates authentication when shared or row credentials change and ignores a late result", async () => {
    const draft = createBulkResourceDraft();
    draft.applyRange("10.0.0.1", "10.0.0.1");
    draft.setShared({ username: "admin", password: "first", authType: "DIGEST" });
    const pending = deferred<void>();
    const authenticate = vi.fn(async () => { await pending.promise; });
    const run = draft.authenticateAll(authenticate);
    draft.updateRow(0, { password: "changed" });
    pending.resolve();
    await run;
    expect(draft.rows[0]?.authentication).toBe("IDLE");
    expect(authenticate).toHaveBeenCalledTimes(1);
  });

  it("limits batch authentication to four concurrent requests", async () => {
    const draft = createBulkResourceDraft();
    draft.applyRange("10.0.0.1", "10.0.0.12");
    draft.setShared({ username: "admin", password: "secret", authType: "DIGEST" });
    let active = 0;
    let maximum = 0;
    await draft.authenticateAll(async () => {
      active += 1;
      maximum = Math.max(maximum, active);
      await new Promise(resolve => setTimeout(resolve, 1));
      active -= 1;
    });
    expect(maximum).toBe(4);
    expect(draft.rows.every(row => row.authentication === "SUCCESS")).toBe(true);
  });

  it("cancels queued authentication after a credential change", async () => {
    const draft = createBulkResourceDraft();
    draft.applyRange("10.0.0.1", "10.0.0.6");
    draft.setShared({ username: "admin", password: "secret", authType: "DIGEST" });
    const pending = deferred<void>();
    const authenticate = vi.fn(async () => { await pending.promise; });
    const run = draft.authenticateAll(authenticate);
    await Promise.resolve();
    draft.updateRow(0, { password: "changed" });
    pending.resolve();
    await run;
    expect(authenticate).toHaveBeenCalledTimes(4);
    expect(draft.rows.every(row => row.authentication !== "AUTHENTICATING")).toBe(true);
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(current => { resolve = current; });
  return { promise, resolve };
}
