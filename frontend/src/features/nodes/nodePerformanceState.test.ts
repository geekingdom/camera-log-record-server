import { describe, expect, it } from "vitest";
import { nodePerformanceDisplay, performanceBudgetDelta } from "./nodePerformanceState";

describe("节点采集性能可读状态", () => {
  it("把没有当前压力但预算尚未恢复的状态显示为等待恢复", () => {
    const display = nodePerformanceDisplay({
      state: "THROTTLED", pressureActive: false, budgetReduced: true, recoveryStableSeconds: 12,
      baseline: { readMiBPerSecond: 50 }, effective: { readMiBPerSecond: 12 },
    });
    expect(display.label).toBe("等待恢复");
    expect(display.detail).toContain("12 秒");
    expect(performanceBudgetDelta({ baseline: { readMiBPerSecond: 50 }, effective: { readMiBPerSecond: 12 } }))
      .toContain("24%");
  });

  it("当前压力优先于历史限流原因", () => {
    expect(nodePerformanceDisplay({ state: "THROTTLED", pressureActive: true, reason: "持续写入延迟压力" }).label)
      .toBe("当前压力");
  });

  it("尚未达到连续压力阈值时明确显示正在累计", () => {
    expect(nodePerformanceDisplay({ state: "NORMAL", pressureActive: true, pressureSamples: 2, reason: "持续写入延迟压力" }))
      .toMatchObject({ label: "压力采样中" });
  });

  it("恢复状态保留恢复中的文案", () => {
    expect(nodePerformanceDisplay({ state: "RECOVERING", pressureActive: false, budgetReduced: true }).label).toBe("恢复中");
  });

  it("最低预算无法继续下调时明确显示预算下限", () => {
    expect(nodePerformanceDisplay({ state: "THROTTLED", pressureActive: false, budgetReduced: false }).label).toBe("预算已达下限");
  });

  it("正常预算显示为已恢复而不是限流中", () => {
    expect(nodePerformanceDisplay({ state: "NORMAL", pressureActive: false, budgetReduced: false }).label).toBe("已恢复");
  });
});
