// 列表、筛选及详情共用状态名称；未知服务端状态保留原值，避免误显示为正常。
export const taskStatusLabels: Record<string, string> = {
  PENDING: "等待调度", CONNECTING: "连接中", COLLECTING: "采集中",
  RECONNECTING: "重连中", STOPPING: "停止中", STOPPED: "已停止",
  PAUSING: "暂停中", PAUSED: "已暂停", WAITING_DEVICE: "等待设备认证", ERROR: "采集失败",
  FAILED: "执行失败", BLOCKED: "等待隔离",
};
export const abnormalStatuses = new Set(["ERROR", "FAILED", "BLOCKED", "RECONNECTING"]);

export function taskStatusLabel(status?: string) {
  return taskStatusLabels[status ?? ""] ?? status ?? "未知";
}

export function taskStatusTone(status?: string): "success" | "warning" | "danger" | "info" {
  if (status === "COLLECTING") return "success";
  if (["ERROR", "FAILED", "BLOCKED"].includes(status ?? "")) return "danger";
  if (status === "RECONNECTING") return "warning";
  return "info";
}
