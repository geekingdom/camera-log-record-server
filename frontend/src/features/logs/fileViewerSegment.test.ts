import { describe, expect, it } from "vitest";
import { completeUtf8Length, firstCompleteUtf8Offset, segmentFetchOffset, validateSegmentResponse } from "./fileViewerSegment";

describe("归档片段边界", () => {
  it("读取任意命中位置时保留有限前置上下文", () => {
    expect(segmentFetchOffset(0)).toBe(0);
    expect(segmentFetchOffset(400)).toBe(272);
    expect(segmentFetchOffset(1200)).toBe(1072);
  });

  it("不把跨页未完成 UTF-8 解码为替代字符", () => {
    expect(completeUtf8Length(Uint8Array.from([0xe4, 0xb8]))).toBe(0);
    expect(completeUtf8Length(Uint8Array.from([0x61, 0xe4, 0xb8, 0xad]))).toBe(4);
    expect(firstCompleteUtf8Offset(Uint8Array.from([0xb8, 0xad, 0x61]))).toBe(2);
  });

  it("拒绝文件身份错误和不推进的有正文响应", () => {
    expect(() => validateSegmentResponse("other", "file", 0, 1, 1, 10)).toThrow("当前文件");
    expect(() => validateSegmentResponse("file", "file", 2, 2, 1, 10)).toThrow("偏移");
    expect(() => validateSegmentResponse("file", "file", 2, 3, 1, 10, "old", "new")).toThrow("会话");
    expect(() => validateSegmentResponse("file", "file", 2, 3, 1, 10)).not.toThrow();
  });
});
