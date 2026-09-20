// 节点归档诊断兼容新旧 Worker 上报，避免浏览器根据未提供的阶段数据作推断。
import type { NodePerformanceStatus } from "../../shared/types";

type Archives = NonNullable<NodePerformanceStatus["archives"]>;

/** 只呈现服务端明确上报的排队和执行事实；旧字段视为未拆分的排队积压。 */
export function archiveDiagnostics(archives?: Archives): string | undefined {
  if (!archives) return undefined;
  if (archives.waitingJobs === undefined && archives.runningJobs === undefined) {
    if (!archives.pendingJobs || archives.pendingJobs <= 0) return undefined;
    const seconds = archives.oldestSeconds;
    return `归档：未完成 ${archives.pendingJobs} 个${seconds == null ? "" : `，最长 ${Math.round(seconds)} 秒`}（旧节点未区分排队与执行）`;
  }
  const waiting = archives.waitingJobs ?? (archives.runningJobs === undefined ? archives.pendingJobs : 0);
  const running = archives.runningJobs;
  const parts: string[] = [];
  if (waiting && waiting > 0) {
    const seconds = archives.oldestWaitingSeconds ?? archives.oldestSeconds;
    parts.push(`排队 ${waiting} 个${seconds == null ? "" : `，最长 ${Math.round(seconds)} 秒`}`);
  }
  if (running && running > 0) {
    const seconds = archives.oldestRunningSeconds;
    parts.push(`执行中 ${running} 个${seconds == null ? "" : `，最长 ${Math.round(seconds)} 秒`}`);
  }
  return parts.length ? `归档：${parts.join("；")}` : undefined;
}
