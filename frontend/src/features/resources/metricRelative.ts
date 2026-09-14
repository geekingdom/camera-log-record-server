// 内存相对趋势仅服务展示：每条曲线独立使用当前查询结果的首个有效采样作固定基准。
import type { MetricSeries } from "./resourceMetrics";

export interface RelativeMetricResult {
  series: MetricSeries[];
  zeroBaselineNames: string[];
}

/**
 * 将每条曲线转换为相对首个有效采样的变化率；图表缩放不会参与计算基准。
 * `null` 和非有限值保留为缺口。零是有效采样却不能作分母，因此该曲线所有有效点
 * 均返回 `null`，由调用方明确提示而不伪装为 0% 增长。
 */
export function relativeMetricSeries(series: MetricSeries[]): RelativeMetricResult {
  const zeroBaselineNames: string[] = [];
  const relative = series.map(item => {
    const baseline = item.points.find(([, value]) => value !== null && Number.isFinite(value))?.[1];
    const hasZeroBaseline = baseline === 0;
    if (hasZeroBaseline) zeroBaselineNames.push(item.name);
    return {
      ...item,
      unit: "%" as const,
      points: item.points.map(([time, value]) => {
        if (value === null || !Number.isFinite(value) || baseline === undefined || baseline === null || hasZeroBaseline) return [time, null] as [number, null];
        return [time, (value - baseline) / baseline * 100] as [number, number];
      }),
    };
  });
  return { series: relative, zeroBaselineNames };
}
