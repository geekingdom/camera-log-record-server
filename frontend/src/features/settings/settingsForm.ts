// 后台容量表单的边界与归一化规则集中管理，避免编辑发现节点时再次截断合法的大容量。
export { DEFAULT_WRITE_LATENCY_LIMIT_MS, MAX_WRITE_LATENCY_LIMIT_MS, MIN_WRITE_LATENCY_LIMIT_MS } from "../../shared/nodeWriteLatency";
export const MAX_NODE_CAPACITY = 10_000;
export const MAX_CLUSTER_CAPACITY = 10_000;

export function normalizeCapacity(value: number, maximum: number): number {
  return Math.min(maximum, Math.max(1, Math.trunc(Number.isFinite(value) ? value : 1)));
}
