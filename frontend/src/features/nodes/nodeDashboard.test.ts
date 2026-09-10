import { describe, expect, it } from "vitest";
import type { Node } from "../../shared/types";
import { currentHealth, currentTelemetry, formatRate, nodeAbnormal, nodeAdmissible, nodeOnline } from "./nodeDashboard";

const now = Date.parse("2026-09-10T00:00:00.000Z");
const activeNode: Node = {
  id: "collector-a", heartbeat: new Date(now - 1_000).toISOString(), accepting: true, capacity: 8, activeTasks: 2,
  telemetry: { sampledAt: new Date(now - 1_000).toISOString(), scope: "HOST", cpuPercent: 42, memoryPercent: 61 },
  health: { status: "HEALTHY", reasons: [] },
};

describe("节点看板状态", () => {
  it("只把新鲜心跳与未满容量的节点计入在线和可准入", () => {
    expect(nodeOnline(activeNode, now)).toBe(true);
    expect(nodeAdmissible(activeNode, now)).toBe(true);
    expect(nodeAdmissible({ ...activeNode, activeTasks: 8 }, now)).toBe(false);
  });
  it("过期遥测不展示资源指标，但保留服务端即时健康原因", () => {
    const stale = { ...activeNode, telemetry: { ...activeNode.telemetry, sampledAt: new Date(now - 16_000).toISOString() }, health: { status: "CRITICAL", reasons: ["节点已隔离"] } };
    expect(currentTelemetry(stale, now)).toBeUndefined();
    expect(currentHealth(stale)).toEqual({ status: "CRITICAL", reasons: ["节点已隔离"] });
  });
  it("离线和严重健康均为异常，速率缺失不回填零值", () => {
    expect(nodeAbnormal({ ...activeNode, heartbeat: new Date(now - 31_000).toISOString() }, now)).toBe(true);
    expect(nodeAbnormal({ ...activeNode, health: { status: "CRITICAL", reasons: ["内存不足"] } }, now)).toBe(true);
    expect(formatRate()).toBe("暂无数据");
  });
  it("UNKNOWN 采样范围保持可解析，服务端未知健康状态不擅自改写", () => {
    const node = { ...activeNode, telemetry: { ...activeNode.telemetry, scope: "UNKNOWN" }, health: { status: "UNKNOWN", reasons: ["网络速率尚未形成连续采样"] } };
    expect(currentTelemetry(node, now)?.scope).toBe("UNKNOWN");
    expect(currentHealth(node)).toEqual(node.health);
  });
  it("服务端硬阻断和同一阈值资源压力不能被旧准入字段绕过", () => {
    expect(nodeAdmissible({ ...activeNode, isolated: true }, now)).toBe(false);
    expect(nodeAdmissible({ ...activeNode, health: { status: "CRITICAL", reasons: ["节点已隔离"] } }, now)).toBe(false);
    expect(nodeAdmissible({ ...activeNode, telemetry: { ...activeNode.telemetry, cpuPercent: 95 } }, now)).toBe(false);
    expect(nodeAdmissible({ ...activeNode, diskPercent: 90 }, now)).toBe(false);
  });
});
