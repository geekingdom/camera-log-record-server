import { describe, expect, it } from "vitest";
import { memoryLabel, memoryPrecision, memoryUnit, metricAxis, percentageLabel } from "./metricAxis";

const series = [{ key: "mem", name: "进程", unit: "KB" as const, points: [[0, 1000] as [number, number], [60_000, 1020] as [number, number], [120_000, 1040] as [number, number]] }];
describe("资源监控动态纵轴", () => {
  it("按可见图例与缩放范围计算带留白的非零内存值域", () => {
    expect(metricAxis(series)).toMatchObject({ min: expect.any(Number), max: expect.any(Number) });
    const zoomed = metricAxis(series, { start: 60_000, end: 120_000 });
    expect(zoomed.min).toBeGreaterThan(900); expect(zoomed.max).toBeLessThan(1100);
    expect(metricAxis(series, { selected: { 进程: false } })).toEqual({ min: 0, max: 1 });
  });
  it("同一图表按可见量级统一 KB 或 MB，内存标签最多显示两位小数", () => {
    expect(memoryLabel(900)).toBe("900 KB");
    const display = memoryUnit(series);
    expect(display.unit).toBe("MB");
    expect(memoryLabel(2048, display)).toBe("2 MB");
    expect(memoryLabel(504_824, display)).toBe("492.99 MB");
    expect(memoryLabel(43_911, display)).toBe("42.88 MB");
    expect(memoryLabel(null, display)).toBe("暂无数据");
    expect(memoryLabel("", display)).toBe("暂无数据");
    expect(memoryLabel("-", display)).toBe("暂无数据");
    expect(percentageLabel(55.179688)).toBe("55.18 %");
    expect(percentageLabel(null)).toBe("暂无数据");
    expect(percentageLabel("-")).toBe("暂无数据");
  });
  it("高基线的小波动不因绝对量级扩大纵轴，并保留不同刻度", () => {
    const near = [{ ...series[0], points: [[0, 300000], [60000, 300010]] as Array<[number, number]> }];
    const bounds = metricAxis(near);
    expect(bounds.min).toBeGreaterThan(299990);
    expect(bounds.max).toBeLessThan(300020);
    const precision = memoryPrecision(bounds, 1024);
    expect(precision).toBe(2);
  });
  it("隐藏高值项和缩放后仅按仍可见的点计算，支持大量点", () => {
    const high = { ...series[0], name: "高内存进程", points: [[0, 1000000]] as Array<[number, number]> };
    expect(metricAxis([...series, high], { selected: { 高内存进程: false } }).max).toBeLessThan(1100);
    const large = { ...series[0], points: Array.from({ length: 200000 }, (_, i) => [i, 1000 + i % 2] as [number, number]) };
    expect(metricAxis([large]).min).toBeGreaterThan(990);
  });
});
