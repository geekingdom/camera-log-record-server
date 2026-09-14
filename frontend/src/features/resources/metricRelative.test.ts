import { describe, expect, it } from "vitest";

import { relativeMetricSeries } from "./metricRelative";

describe("资源内存相对首个有效采样比率", () => {
  it("每条曲线以当前查询范围内首个有效采样为100%，保留原有缺口", () => {
    const result = relativeMetricSeries([
      { key: "main", name: "主进程", unit: "KB" as const, points: [[0, null], [60_000, 200], [120_000, null], [180_000, 250]] },
      { key: "worker", name: "工作进程", unit: "KB" as const, points: [[0, 400], [60_000, 380]] },
    ]);

    expect(result.series).toEqual([
      { key: "main", name: "主进程", unit: "%", points: [[0, null], [60_000, 100], [120_000, null], [180_000, 125]] },
      { key: "worker", name: "工作进程", unit: "%", points: [[0, 100], [60_000, 95]] },
    ]);
    expect(result.zeroBaselineNames).toEqual([]);
  });

  it("跳过非有限值寻找首个有效基准，但零值是有效采样且不会另找非零基准", () => {
    const result = relativeMetricSeries([
      { key: "zero", name: "零基准", unit: "KB" as const, points: [[0, null], [60_000, Number.NaN], [120_000, 0], [180_000, 10], [240_000, null]] },
      { key: "finite", name: "有效基准", unit: "KB" as const, points: [[0, Number.POSITIVE_INFINITY], [60_000, 50], [120_000, 75]] },
    ]);

    expect(result.series[0]?.points).toEqual([[0, null], [60_000, null], [120_000, null], [180_000, null], [240_000, null]]);
    expect(result.series[1]?.points).toEqual([[0, null], [60_000, 100], [120_000, 150]]);
    expect(result.zeroBaselineNames).toEqual(["零基准"]);
  });

  it("不根据图表缩放重算基准，调用方可仅改变纵轴窗口", () => {
    const result = relativeMetricSeries([
      { key: "main", name: "主进程", unit: "KB" as const, points: [[0, 100], [60_000, 120], [120_000, 140]] },
    ]);

    expect(result.series[0]?.points).toEqual([[0, 100], [60_000, 120], [120_000, 140]]);
  });
});
