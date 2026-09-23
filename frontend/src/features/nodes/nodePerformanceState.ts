import type { NodePerformanceStatus } from "../../shared/types";

export type NodePerformanceDisplay = {
  label: string;
  tone: "success" | "warning" | "info";
  detail: string;
};

/** 将控制器状态和当前压力拆开呈现，避免历史限流被误读为当前故障。 */
export function nodePerformanceDisplay(status?: NodePerformanceStatus | null): NodePerformanceDisplay {
  if (!status) return { label: "未上报", tone: "info", detail: "节点尚未上报性能预算" };
  if (status.state === "MANUAL") {
    return { label: "手动参数", tone: "success", detail: "使用管理员保存的固定性能预算" };
  }
  if (status.pressureActive && status.state === "NORMAL") {
    const samples = Number(status.pressureSamples ?? 0);
    return {
      label: "压力采样中",
      tone: "warning",
      detail: status.reason ? `检测到：${status.reason}；已累计 ${samples} 次，尚未触发自动降速` : "检测到压力，正在累计采样，尚未触发自动降速",
    };
  }
  if (status.pressureActive) {
    return {
      label: "当前压力",
      tone: "warning",
      detail: status.reason ? `正在检测：${status.reason}` : "正在检测持续性能压力",
    };
  }
  if (status.state === "THROTTLED" && status.budgetReduced) {
    const stable = Number(status.recoveryStableSeconds ?? 0);
    return {
      label: stable > 0 ? "等待恢复" : "限流已保持",
      tone: "warning",
      detail: stable > 0 ? `当前无持续压力，已稳定 ${stable} 秒，预算将逐步恢复` : "当前无持续压力，仍保持收紧后的预算",
    };
  }
  if (status.state === "THROTTLED" && !status.budgetReduced && !status.pressureActive) {
    return { label: "预算已达下限", tone: "warning", detail: "当前无持续压力，已使用可用的最小后台预算" };
  }
  if (status.state === "RECOVERING") {
    return { label: "恢复中", tone: "info", detail: "性能压力已消失，正在逐步恢复预算" };
  }
  if (status.state === "NORMAL") {
    return { label: "已恢复", tone: "success", detail: "当前有效预算已满足基准" };
  }
  return { label: status.state || "未上报", tone: "info", detail: status.reason || "暂无性能状态说明" };
}

export function performanceBudgetDelta(status?: NodePerformanceStatus | null): string | undefined {
  const effective = status?.effective?.readMiBPerSecond;
  const baseline = status?.baseline?.readMiBPerSecond;
  if (effective == null || baseline == null || effective >= baseline) return undefined;
  return `后台读取 ${effective} / ${baseline} MiB/s（当前为基准的 ${Math.round(effective / baseline * 100)}%）`;
}
