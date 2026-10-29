import { describe, expect, it } from "vitest";
import { segmentTime, visibleSegments } from "./downloadArtifactSegments";

describe("downloadArtifactSegments", () => {
  it("旧分卷缺少实际接收时间时仍展示已知逻辑小时", () => {
    expect(segmentTime({ fileId: "legacy", hour: "2026-10-29T00:00:00+00:00" })).toContain("逻辑小时：2026/10/29 08:00:00");
  });

  it("默认仅展示前两个分卷，展开后返回完整明细", () => {
    const segments = [
      { fileId: "a", hour: "2026-10-29T00:00:00+00:00" },
      { fileId: "b", hour: "2026-10-29T01:00:00+00:00" },
      { fileId: "c", hour: "2026-10-29T02:00:00+00:00" },
    ];
    expect(visibleSegments(segments, false)).toHaveLength(2);
    expect(visibleSegments(segments, true)).toHaveLength(3);
  });
});
