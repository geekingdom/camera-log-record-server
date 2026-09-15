// 节点写入延迟限制在配置页和运行看板共用，缺省值须与服务端历史配置兼容。
import type { Node } from "./types";

export const MIN_WRITE_LATENCY_LIMIT_MS = 1;
export const MAX_WRITE_LATENCY_LIMIT_MS = 60_000;
export const DEFAULT_WRITE_LATENCY_LIMIT_MS = 200;

/** 旧节点未返回限制时，按服务端约定回退为 200 ms。 */
export function writeLatencyLimitMs(node: Pick<Node, "writeLatencyLimitMs">) {
  return Number(node.writeLatencyLimitMs ?? DEFAULT_WRITE_LATENCY_LIMIT_MS);
}

/** 将毫秒阈值保持为节点卡片和设置表格一致的紧凑文本。 */
export function formatWriteLatencyLimit(limit?: number | null) {
  const value = Number(limit ?? DEFAULT_WRITE_LATENCY_LIMIT_MS);
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(value % 1000 ? 1 : 0)} s`;
}
