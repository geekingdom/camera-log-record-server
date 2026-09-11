// 单图纵轴按当前可见系列和缩放时间窗计算，避免内存曲线被固定零点压扁。
import type { MetricSeries } from "./resourceMetrics";

export interface AxisWindow { start?: number; end?: number; selected?: Record<string, boolean>; }

/** 返回包含适度留白的值域；单点和恒定值会扩展为稳定可读的范围。 */
export function metricAxis(series: MetricSeries[], window: AxisWindow = {}) {
  let low = Infinity, high = -Infinity;
  for (const item of series) {
    if (window.selected?.[item.name] === false) continue;
    for (const [time, value] of item.points) {
      if (value === null || !Number.isFinite(value) || (window.start !== undefined && time < window.start)
          || (window.end !== undefined && time > window.end)) continue;
      low = Math.min(low, value); high = Math.max(high, value);
    }
  }
  if (!Number.isFinite(low)) return { min: 0, max: 1 };
  const span = high - low;
  const padding = span ? Math.max(span * 0.12, 0.01) : Math.max(Math.abs(high) * 0.005, 1);
  return { min: low - padding, max: high + padding };
}

/** 内存刻度以 KB 为输入，按量级转换为 KB、MB 或 GB，保留紧凑有效位。 */
export function memoryUnit(series: MetricSeries[], window: AxisWindow = {}) {
  const { max } = metricAxis(series, window); const magnitude = Math.abs(max);
  return magnitude >= 1024 * 1024 ? { factor: 1024 * 1024, unit: "GB" } : magnitude >= 1024 ? { factor: 1024, unit: "MB" } : { factor: 1, unit: "KB" };
}
export function memoryLabel(value: number, display = { factor: 1, unit: "KB" }): string {
  const { factor, unit } = display;
  return `${(value / factor).toLocaleString("zh-CN", { maximumFractionDigits: 6 })} ${unit}`;
}
/** 值域越窄，刻度保留越多小数，避免 MB 级小波动被格式化为同一个整数。 */
export function memoryPrecision(bounds: { min: number; max: number }, factor: number): number {
  const step = Math.abs(bounds.max - bounds.min) / (5 * factor);
  return Math.max(0, Math.min(6, step >= 1 ? 1 : Math.ceil(-Math.log10(Math.max(step, 1e-9))) + 1));
}
