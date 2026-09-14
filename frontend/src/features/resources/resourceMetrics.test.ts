import { describe, expect, it } from "vitest";

import { metricSeries, metricSummaries, metricsCsv, validMetricRange } from "./resourceMetrics";

const samples = [
  { sampledAt: "2026-09-11T00:01:00.000Z", status: "OK", values: [{ id: "cpu", name: "CPU", value: 12, unit: "%" as const }, { id: "mem", name: "进程, A", pid: 4, value: 1024, unit: "KB" as const }] },
  { sampledAt: "2026-09-11T00:02:00.000Z", status: "OK", values: [{ id: "cpu", name: "CPU", value: 18, unit: "%" as const }, { id: "mem", name: "进程, A", pid: 4, value: 1124, unit: "KB" as const }] },
];

describe("资源监控数据转换", () => {
  it("按单位和资源身份分轴聚合，并计算最新变化", () => {
    const cpu = metricSeries(samples, "%");
    const memory = metricSeries(samples, "KB");
    expect(cpu).toEqual([{ key: '["metric","cpu","CPU",{"model":"","subSerialNumber":""}]', unit: "%", name: "CPU", points: [[Date.parse(samples[0].sampledAt), 12], [Date.parse(samples[1].sampledAt), 18]] }]);
    expect(memory[0]).toMatchObject({ name: "进程, A" });
    expect(memory[0]).not.toHaveProperty("pid");
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

  it("将同一采样中的旧版和新版同名进程合并，且不跨采样累计", () => {
    const processSamples = [
      {
        sampledAt: "2026-09-11T00:01:00.000Z",
        status: "OK",
        identity: { model: "M1", subSerialNumber: "S1" },
        values: [
          { id: "mem", name: "采集服务", pid: 41, value: 100, unit: "KB" as const },
          { id: "mem", name: "采集服务", pid: 42, value: 200, unit: "KB" as const },
          { id: "process:采集服务", name: "采集服务", value: 50, unit: "KB" as const },
        ],
      },
      {
        sampledAt: "2026-09-11T00:02:00.000Z",
        status: "OK",
        identity: { model: "M1", subSerialNumber: "S1" },
        values: [{ id: "mem", name: "采集服务", pid: 43, value: 350, unit: "KB" as const }],
      },
    ];
    const series = metricSeries(processSamples, "KB");
    expect(series).toHaveLength(1);
    expect(series[0]).toMatchObject({ name: "采集服务" });
    expect(series[0]).not.toHaveProperty("pid");
    expect(series[0]?.points).toEqual([
      [Date.parse(processSamples[0].sampledAt), 350],
      [Date.parse(processSamples[1].sampledAt), 350],
    ]);
  });

  it("按设备身份分隔进程曲线，并为缺失的进程保留空点", () => {
    const processSamples = [
      {
        sampledAt: "2026-09-11T00:01:00.000Z",
        status: "OK",
        identity: { model: "M1", subSerialNumber: "S1" },
        values: [{ id: "process:采集服务", name: "采集服务", value: 100, unit: "KB" as const }],
      },
      {
        sampledAt: "2026-09-11T00:02:00.000Z",
        status: "OK",
        identity: { model: "M1", subSerialNumber: "S1" },
        values: [{ id: "cpu", name: "CPU", value: 1, unit: "%" as const }],
      },
      {
        sampledAt: "2026-09-11T00:03:00.000Z",
        status: "OK",
        identity: { model: "M2", subSerialNumber: "S2" },
        values: [{ id: "process:采集服务", name: "采集服务", value: 200, unit: "KB" as const }],
      },
    ];
    const series = metricSeries(processSamples, "KB");
    expect(series).toHaveLength(2);
    expect(series[0]?.key).not.toBe(series[1]?.key);
    expect(series.map(item => item.name)).toEqual([
      '采集服务 [型号="M1"; 序列号="S1"]',
      '采集服务 [型号="M2"; 序列号="S2"]',
    ]);
    expect(series[0]?.points[1]).toEqual([Date.parse(processSamples[1].sampledAt), null]);
  });

  it("同一设备的同名普通指标不附加 PID 或设备标识", () => {
    const deviceSamples = [{
      sampledAt: "2026-09-11T00:01:00.000Z",
      status: "OK",
      identity: { model: "M1", subSerialNumber: "S1" },
      values: [{ id: "mem", name: "内存", value: 100, unit: "KB" as const }],
    }];
    const series = metricSeries(deviceSamples, "KB");
    expect(series[0]?.name).toBe("内存");
    expect(series[0]?.name).not.toContain("PID");
  });

  it("导出时合并同名进程并隐藏 PID", () => {
    const csv = metricsCsv([{
      sampledAt: "2026-09-11T00:01:00.000Z",
      status: "OK",
      values: [
        { id: "mem", name: "采集服务", pid: 41, value: 100, unit: "KB" as const },
        { id: "mem", name: "采集服务", pid: 42, value: 200, unit: "KB" as const },
      ],
    }]);
    const lines = csv.split("\r\n");
    expect(lines).toHaveLength(2);
    expect(lines[0]).not.toContain('"pid"');
    expect(lines[1]).toContain('"process:采集服务","采集服务","300","KB"');
  });
});
