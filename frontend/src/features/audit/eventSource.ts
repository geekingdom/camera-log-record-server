// 统一事件来源两行展示，服务端派生信息优先，旧数据缺失须明确而不猜测操作人。
import type { EventBase } from "./api";

/** 来源不等于用户操作人；节点自动事件由服务端根据原始记录给出来源。 */
export function eventSource(row: EventBase): { name: string; detail: string } {
  return {
    name: row.sourceName || String(row.actorName || row.actor || "历史记录缺少身份"),
    detail: row.sourceDetail || row.clientIp || "来源地址未保存",
  };
}
