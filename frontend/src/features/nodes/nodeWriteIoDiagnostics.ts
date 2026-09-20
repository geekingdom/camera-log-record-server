// 实际写入诊断只消费服务端 tracker 指标；它不包含主动批等待，不能与 writeIo 阶段统计混用。
import type { NodePerformanceStatus } from "../../shared/types";

type WriteIoLatency = NonNullable<NodePerformanceStatus["writeIoLatency"]>;

/** 保留缺失字段为未知，避免把尚未上报的实际写入延迟渲染为零。 */
export function writeIoDiagnostics(latency?: WriteIoLatency): string | undefined {
  if (!latency) return undefined;
  const values: string[] = [];
  if (finite(latency.p99Ms)) values.push(`实际写入 P99：${latency.p99Ms.toFixed(1)} ms`);
  if (finite(latency.pendingMs)) values.push(`未完成写入等待：${Math.round(latency.pendingMs)} ms`);
  if (!values.length) return undefined;
  const details: string[] = [];
  if (finite(latency.windowSeconds)) details.push(`${Math.round(latency.windowSeconds)} 秒窗口`);
  if (finite(latency.samples)) details.push(`${Math.round(latency.samples)} 个样本`);
  return `${values.join("；")}${details.length ? `（${details.join("，")}）` : ""}`;
}

function finite(value: number | undefined): value is number { return typeof value === "number" && Number.isFinite(value); }
