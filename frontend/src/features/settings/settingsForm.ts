// 后台容量表单的边界与归一化规则集中管理，避免编辑发现节点时再次截断合法的大容量。
export { DEFAULT_WRITE_LATENCY_LIMIT_MS, MAX_WRITE_LATENCY_LIMIT_MS, MIN_WRITE_LATENCY_LIMIT_MS } from "../../shared/nodeWriteLatency";
export const MAX_NODE_CAPACITY = 10_000;
export const MAX_CLUSTER_CAPACITY = 10_000;
export const MIN_LOG_SEGMENT_MIB = 10;
export const MAX_STANDARD_LOG_SEGMENT_MIB = 510;
export const MAX_LOG_SEGMENT_MIB = 512;
export const LOG_SEGMENT_STEP_MIB = 5;

export function normalizeCapacity(value: number, maximum: number): number {
  return Math.min(maximum, Math.max(1, Math.trunc(Number.isFinite(value) ? value : 1)));
}

export function isValidLogSegmentMiB(value: number): boolean {
  return Number.isInteger(value)
    && (value === MAX_LOG_SEGMENT_MIB
      || (value >= MIN_LOG_SEGMENT_MIB && value <= MAX_STANDARD_LOG_SEGMENT_MIB
        && (value - MIN_LOG_SEGMENT_MIB) % LOG_SEGMENT_STEP_MIB === 0));
}
