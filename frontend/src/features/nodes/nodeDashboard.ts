// 节点看板的纯展示判断集中于此；资源遥测时效与服务端健康结论保持独立。
import type { Node, NodeHealth, NodeTelemetry } from "../../shared/types";
import { writeLatencyLimitMs } from "../../shared/nodeWriteLatency";

export { writeLatencyLimitMs } from "../../shared/nodeWriteLatency";

export const HEARTBEAT_STALE_MS = 30_000;
export const TELEMETRY_STALE_MS = 15_000;
const CLOCK_SKEW_TOLERANCE_MS = 2_000;

function timestamp(value?: string | null) {
  const parsed = value ? new Date(value).getTime() : Number.NaN;
  return Number.isFinite(parsed) ? parsed : undefined;
}

/** 节点心跳采用服务端三十秒阈值，并容忍两秒时钟偏差。 */
export function nodeOnline(node: Node, current = Date.now()) {
  const reportedAt = timestamp(node.heartbeat);
  return reportedAt !== undefined && current - reportedAt >= -CLOCK_SKEW_TOLERANCE_MS && current - reportedAt <= HEARTBEAT_STALE_MS;
}

/** 遥测采用服务端十五秒阈值；过期数据只保留服务端健康原因，不作为资源仪表。 */
export function currentTelemetry(node: Node, current = Date.now()): NodeTelemetry | undefined {
  const telemetry = node.telemetry;
  const sampledAt = timestamp(telemetry?.sampledAt);
  if (!telemetry || sampledAt === undefined || current - sampledAt < -CLOCK_SKEW_TOLERANCE_MS || current - sampledAt > TELEMETRY_STALE_MS) return undefined;
  return telemetry;
}

/** 健康由服务端即时合成，隔离和磁盘等原因不能被旧遥测隐藏。 */
export function currentHealth(node: Node): NodeHealth | undefined {
  return node.health;
}

/** 仅以现有的准入信号判断是否可接收新任务，不擅自推导调度评分。 */
export function nodeAdmissible(node: Node, current = Date.now()) {
  const capacity = Number(node.capacity ?? 0);
  const activeTasks = Number(node.activeTasks ?? 0);
  const telemetry = currentTelemetry(node, current);
  const pressured = Number(node.diskPercent ?? 0) >= 90 || Number(node.writeLatencyMs ?? 0) > writeLatencyLimitMs(node) ||
    Number(telemetry?.cpuPercent ?? 0) >= 95 || Number(telemetry?.memoryPercent ?? 0) >= 95;
  return nodeOnline(node, current) && node.accepting === true && !node.isolated && !node.configurationMismatch &&
    currentHealth(node)?.status !== "CRITICAL" && !pressured && capacity > activeTasks;
}

/** 需要人工关注的节点包括失联、配置不一致和服务端给出的告警/严重健康状态。 */
export function nodeAbnormal(node: Node, current = Date.now()) {
  if (!nodeOnline(node, current) || node.configurationMismatch) return true;
  return ["WARNING", "CRITICAL", "OFFLINE"].includes(String(currentHealth(node)?.status));
}

export function percentage(value?: number | null) {
  if (!Number.isFinite(value)) return undefined;
  return Math.max(0, Math.min(100, Number(value!.toFixed(1))));
}

export function formatBytes(value?: number | null) {
  if (!Number.isFinite(value) || value! < 0) return "暂无数据";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value!;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount >= 100 || index === 0 ? Math.round(amount) : amount.toFixed(1)} ${units[index]}`;
}

export function formatRate(value?: number | null) {
  const formatted = formatBytes(value);
  return formatted === "暂无数据" ? formatted : `${formatted}/s`;
}

export function formatLatency(node: Node) {
  const samples = Number(node.writeLatencySamples ?? 0);
  const pending = Number(node.writeLatencyPendingMs ?? 0);
  const latency = Math.max(Number(node.writeLatencyMs ?? 0), pending);
  if (!samples && !pending) return "暂无数据";
  if (latency < 1000) return `${Math.round(latency)} ms`;
  if (latency < 60_000) return `${(latency / 1000).toFixed(1)} s`;
  return `${(latency / 60_000).toFixed(1)} min`;
}
