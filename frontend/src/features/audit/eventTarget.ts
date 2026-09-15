// 请求集合与实体对象使用不同说明，避免把不适用的单个设备标识称为未记录。
import type { EventBase } from "./api";

/** 优先展示实体名称，其次展示平台提供的对象类别。 */
export function requestTargetName(row: EventBase): string {
  return String(row.targetName || row.taskName || row.targetLabel || row.route || "平台接口");
}

/** 保留实体 ID 或设备 IP；集合和平台操作明确说明适用范围。 */
export function requestTargetDetail(row: EventBase): string {
  const identifier = row.deviceIp || row.targetId || row.taskId || row.resourceId || row.nodeId;
  if (identifier) return String(identifier);
  if (row.targetScope === "COLLECTION") return row.method === "GET" ? "集合查询（无单个对象）" : "集合操作（无路径对象）";
  if (row.targetScope === "PLATFORM") return "平台级接口";
  return "该记录未保存对象标识";
}
