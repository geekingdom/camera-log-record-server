/**
 * 任务列表的生命周期操作矩阵。
 *
 * 状态过渡期间只暴露能够改变当前意图的操作，避免轮询尚未反映结果时重复提交。
 * 权限和最终状态合法性仍由服务端 API 校验，列表仅根据公开任务快照减少无效入口。
 */
import type { Task } from "../../shared/types";

export type TaskAction = "start" | "stop" | "pause" | "resume";

const transitionalRunningStatuses = new Set(["PENDING", "CONNECTING", "RECONNECTING"]);

/** 根据任务的实际状态、期望状态和协议返回当前可发起的生命周期操作。 */
export function availableTaskActions(task: Task): TaskAction[] {
  const status = task.status ?? "";
  const desiredState = task.desiredState ?? "";

  if (status === "PAUSING" || status === "STOPPING") return [];
  if (status === "PAUSED" && desiredState === "PAUSED" && task.protocol === "SSH") {
    return ["resume", "stop"];
  }
  if (status === "COLLECTING" && desiredState === "RUNNING") {
    return task.protocol === "SSH" ? ["pause", "stop"] : ["stop"];
  }
  if (transitionalRunningStatuses.has(status) && desiredState === "RUNNING") return ["stop"];
  if (status === "STOPPED" && desiredState === "STOPPED") return ["start"];
  if (status === "ERROR" && desiredState === "STOPPED" && task.nodeId == null) return ["start"];
  return [];
}

/**
 * 从当前页面的任务快照筛出可执行某生命周期操作的子集。
 *
 * 列表按钮和批量操作都必须复用同一状态矩阵；资源已删除的任务即使仍可查询，
 * 也不能再提交新的控制意图。
 */
export function applicableTaskActions(tasks: Task[], action: TaskAction) {
  const applicable = tasks.filter(task =>
    !task.resourceDeleted && availableTaskActions(task).includes(action),
  );
  return { applicable, skipped: tasks.filter(task => !applicable.includes(task)) };
}
