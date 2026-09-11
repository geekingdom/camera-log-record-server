import { describe, expect, it } from "vitest";
import { findTextMatches, tokenizeLogText } from "./logText";

describe("实时日志文本处理", () => {
  it("保留查询空格及 Unicode 原文偏移，特殊字符作为字面查询", () => {
    expect(findTextMatches("İ ERROR [x] ", "ERROR")).toEqual([{ start: 2, end: 7 }]);
    expect(findTextMatches("a b", " ")).toEqual([{ start: 1, end: 2 }]);
    expect(findTextMatches("a[x]b", "[x]")).toEqual([{ start: 1, end: 4 }]);
    expect(findTextMatches("a".repeat(1500), "a", Infinity)).toHaveLength(1500);
    expect(tokenizeLogText("a".repeat(1500), "a", 1499).at(-1)?.highlighted).toBe(true);
  });
  it("按大小写无关的字面内容返回不重叠匹配位置", () => {
    expect(findTextMatches("Error error ERROR", "error")).toEqual([
      { start: 0, end: 5 },
      { start: 6, end: 11 },
      { start: 12, end: 17 },
    ]);
  });

  it("将空查询和无匹配保持为空，并限制高频匹配数量", () => {
    expect(findTextMatches("trace", "")).toEqual([]);
    expect(findTextMatches("trace", "missing")).toEqual([]);
    expect(findTextMatches("a".repeat(1500), "a")).toHaveLength(1000);
  });

  it("先移除终端控制码，再保留搜索高亮和级别颜色边界", () => {
    expect(tokenizeLogText("\u001B[31mERROR\u001B[0m disk", "disk")).toEqual([
      { text: "ERROR", tone: "error", highlighted: false },
      { text: " ", tone: undefined, highlighted: false },
      { text: "disk", tone: undefined, highlighted: true },
    ]);
  });
});
