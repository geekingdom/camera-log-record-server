import { describe, expect, it } from "vitest";
import { writeIoDiagnostics } from "./nodeWriteIoDiagnostics";

describe("节点实际写入诊断", () => {
  it("把实际写入 P99 与未完成写入等待和主动批等待分开呈现", () => {
    expect(writeIoDiagnostics({ p99Ms: 512.36, pendingMs: 1998.8, samples: 24, windowSeconds: 60 }))
      .toBe("实际写入 P99：512.4 ms；未完成写入等待：1999 ms（60 秒窗口，24 个样本）");
  });

  it("不为缺失服务端字段补零", () => {
    expect(writeIoDiagnostics()).toBeUndefined();
    expect(writeIoDiagnostics({ pendingMs: 2000 })).toBe("未完成写入等待：2000 ms");
  });
});
