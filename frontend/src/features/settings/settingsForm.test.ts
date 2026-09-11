import { describe, expect, it } from "vitest";
import { MAX_CLUSTER_CAPACITY, MAX_NODE_CAPACITY, normalizeCapacity } from "./settingsForm";

describe("后台容量配置", () => {
  it("允许节点容量超过旧的 100 上限并限制在平台最大值内", () => {
    expect(normalizeCapacity(101, MAX_NODE_CAPACITY)).toBe(101);
    expect(normalizeCapacity(10000, MAX_NODE_CAPACITY)).toBe(10000);
    expect(normalizeCapacity(10001, MAX_NODE_CAPACITY)).toBe(10000);
  });

  it("允许集群总上限达到配置契约的 10000", () => {
    expect(normalizeCapacity(101, MAX_CLUSTER_CAPACITY)).toBe(101);
    expect(normalizeCapacity(10000, MAX_CLUSTER_CAPACITY)).toBe(10000);
    expect(normalizeCapacity(10001, MAX_CLUSTER_CAPACITY)).toBe(10000);
  });
});
