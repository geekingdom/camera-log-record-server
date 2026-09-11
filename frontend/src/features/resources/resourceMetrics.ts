// 资源监控视图的纯数据转换：按稳定指标身份聚合，图表层不保存或推断设备原始命令输出。
import type { ResourceMetricSample, ResourceMetricValue } from "../../shared/types";

export type MetricUnit = ResourceMetricValue["unit"];
export interface MetricSeries {
  key: string;
  name: string;
  unit: MetricUnit;
  pid?: number;
  points: Array<[number, number | null]>;
}
export interface MetricSummary {
  value: number | null;
  delta: number | null;
}

/** 将样本按完整展示身份聚合；缺失采样和身份变更均以空点断开曲线。 */
export function metricSeries(samples: ResourceMetricSample[], unit: MetricUnit): MetricSeries[] {
  const ordered = [...samples].sort((left, right) => Date.parse(left.sampledAt) - Date.parse(right.sampledAt));
  const timestamps = ordered.map(sample => Date.parse(sample.sampledAt));
  const values = new Map<string, { name: string; pid?: number; points: Map<number, number> }>();
  ordered.forEach(sample => sample.values.filter(value => value.unit === unit).forEach(value => {
    const identity = (sample as ResourceMetricSample & { identity?: { model?: string; subSerialNumber?: string } }).identity;
    const key = `${value.id}:${value.name}:${value.pid ?? ""}:${identity?.model ?? ""}:${identity?.subSerialNumber ?? ""}`;
    const existing = values.get(key) ?? { name: value.name, pid: value.pid, points: new Map<number, number>() };
    existing.points.set(Date.parse(sample.sampledAt), value.value);
    values.set(key, existing);
  }));
  return [...values.entries()].map(([key, value]) => {
    const aligned = timestamps.map(timestamp => [timestamp, value.points.get(timestamp) ?? null] as [number, number | null]);
    const points: Array<[number, number | null]> = [];
    aligned.forEach((point, index) => {
      const previous = aligned[index - 1];
      // 服务端一分钟采样；中间无任何样本时补两个 null，避免 ECharts 跨长时间空档连线。
      if (previous && previous[1] !== null && point[1] !== null && point[0] - previous[0] > 90_000) {
        points.push([previous[0] + 1, null], [point[0] - 1, null]);
      }
      points.push(point);
    });
    return { key, unit, name: value.pid === undefined ? value.name : `${value.name} (PID ${value.pid})`, pid: value.pid, points };
  });
}

/** 以单条曲线的最新有效点与前一有效点计算变化量，绝不跨指标混合数值。 */
export function metricSummary(series: MetricSeries): MetricSummary {
  const values = series.points.map(point => point[1]).filter((value): value is number => value !== null);
  if (!values.length) return { value: null, delta: null };
  const latest = values.at(-1) ?? null;
  const previous = values.length > 1 ? values.at(-2) ?? null : null;
  return { value: latest, delta: latest !== null && previous !== null ? latest - previous : null };
}

/** 逐项摘要保留指标身份，供界面分别展示 CPU、内存和不同进程的当前值。 */
export function metricSummaries(series: MetricSeries[]) {
  return series.map(item => ({ ...item, ...metricSummary(item) }));
}

/** 约束自定义范围不超过服务端允许的 31 天，调用方据此提示用户修正输入。 */
export function validMetricRange(start: Date, end: Date, now = new Date()): boolean {
  const maximum = 31 * 24 * 60 * 60 * 1000;
  return Number.isFinite(start.getTime()) && Number.isFinite(end.getTime()) && start < end && end <= now && end.getTime() - start.getTime() <= maximum;
}

/** 将结构化样本导出为可被表格软件读取的 RFC 4180 风格 CSV。 */
export function metricsCsv(samples: ResourceMetricSample[]): string {
  const escape = (value: string | number | undefined | null) => `"${String(value ?? "").replace(/"/g, '""')}"`;
  const lines: Array<Array<string | number>> = [["sampledAt", "status", "id", "name", "pid", "value", "unit", "errorCode"]];
  [...samples].sort((left, right) => Date.parse(left.sampledAt) - Date.parse(right.sampledAt)).forEach(sample => {
    if (!sample.values.length) lines.push([sample.sampledAt, sample.status, "", "", "", "", "", sample.errorCode ?? ""]);
    sample.values.forEach(value => lines.push([
      sample.sampledAt, sample.status, value.id, value.name, value.pid ?? "", value.value, value.unit, sample.errorCode ?? "",
    ]));
  });
  return lines.map(line => line.map(escape).join(",")).join("\r\n");
}

/** 从错误和空值样本生成可读摘要，正常空数据由调用方展示为空态。 */
export function metricIssueCount(samples: ResourceMetricSample[]): number {
  return samples.filter(sample => sample.status !== "OK" || Boolean(sample.errorCode)).length;
}
