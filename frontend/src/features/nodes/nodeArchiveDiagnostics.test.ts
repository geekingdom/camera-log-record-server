import { describe, expect, it } from "vitest";
import { archiveDiagnostics } from "./nodeArchiveDiagnostics";

describe("节点归档诊断", () => {
  it("分别呈现排队和执行中的数量及最长时长", () => {
    expect(archiveDiagnostics({ pendingJobs: 5, waitingJobs: 3, runningJobs: 2, oldestWaitingSeconds: 12.6, oldestRunningSeconds: 45.2 }))
      .toBe("归档：排队 3 个，最长 13 秒；执行中 2 个，最长 45 秒");
  });

  it("兼容旧 Worker 的总数和总等待时长", () => {
    expect(archiveDiagnostics({ pendingJobs: 4, oldestSeconds: 18.4 }))
      .toBe("归档：未完成 4 个，最长 18 秒（旧节点未区分排队与执行）");
  });

  it("没有排队或执行任务时不渲染诊断", () => {
    expect(archiveDiagnostics({ pendingJobs: 0 })).toBeUndefined();
  });
});
