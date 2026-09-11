import { describe, expect, it } from "vitest";

import { metricSeries, metricSummaries, metricsCsv, validMetricRange } from "./resourceMetrics";

const samples = [
  { sampledAt: "2026-09-11T00:01:00.000Z", status: "OK", values: [{ id: "cpu", name: "CPU", value: 12, unit: "%" as const }, { id: "mem", name: "进程, A", pid: 4, value: 1024, unit: "KB" as const }] },
  { sampledAt: "2026-09-11T00:02:00.000Z", status: "OK", values: [{ id: "cpu", name: "CPU", value: 18, unit: "%" as const }, { id: "mem", name: "进程, A", pid: 4, value: 1124, unit: "KB" as const }] },
];

describe("资源监控数据转换", () => {
  it("按单位和进程身份分轴聚合，并计算最新变化", () => {
    const cpu = metricSeries(samples, "%");
    const memory = metricSeries(samples, "KB");
    expect(cpu).toEqual([{ key: "cpu:CPU:::", unit: "%", name: "CPU", pid: undefined, points: [[Date.parse(samples[0].sampledAt), 12], [Date.parse(samples[1].sampledAt), 18]] }]);
    expect(memory[0]?.name).toBe("进程, A (PID 4)");
    expect(metricSummaries(cpu)).toMatchObject([{ name: "CPU", value: 18, delta: 6 }]);
  });

  it("名称或 PID 变化、以及缺失一分钟采样时必须断开曲线", () => {
    const changed = metricSeries([
      { sampledAt: "2026-09-11T00:00:00.000Z", status: "OK", values: [{ id: "proc", name: "旧进程", value: 1, unit: "KB" as const }] },
      { sampledAt: "2026-09-11T00:03:00.000Z", status: "OK", values: [{ id: "proc", name: "新进程", value: 2, unit: "KB" as const }] },
    ], "KB");
    expect(changed).toHaveLength(2);
    const missing = metricSeries([
      { sampledAt: "2026-09-11T00:00:00.000Z", status: "OK", values: [{ id: "cpu", name: "CPU", value: 1, unit: "%" as const }] },
      { sampledAt: "2026-09-11T00:03:00.000Z", status: "OK", values: [{ id: "cpu", name: "CPU", value: 2, unit: "%" as const }] },
    ], "%");
    expect(missing[0]?.points.some(([, value]) => value === null)).toBe(true);
  });

  it("拒绝超过 31 天或未来的自定义范围", () => {
    const now = new Date("2026-09-11T00:00:00.000Z");
    expect(validMetricRange(new Date("2026-08-11T00:00:00.000Z"), now, now)).toBe(true);
    expect(validMetricRange(new Date("2026-08-10T23:59:59.000Z"), now, now)).toBe(false);
    expect(validMetricRange(new Date("2026-09-10T00:00:00.000Z"), new Date("2026-09-11T00:01:00.000Z"), now)).toBe(false);
  });

  it("导出时引用逗号和双引号", () => {
    const csv = metricsCsv([{ ...samples[0], values: [{ ...samples[0].values[1], name: '进程, "A"' }] }]);
    expect(csv).toContain('"进程, ""A"""');
    expect(csv.split("\r\n")).toHaveLength(2);
  });
});
