import { describe, expect, it } from "vitest";
import { LiveLogBuffer } from "./composables/liveLogBuffer";
import { TerminalDisplayCleaner, stripTerminalControls } from "./terminalDisplay";

describe("stripTerminalControls", () => {
  it("removes complete ANSI CSI sequences from display text", () => {
    expect(stripTerminalControls("\u001B[1;31mERROR\u001B[0m")).toBe("ERROR");
  });

  it("removes other recognized ANSI control sequences", () => {
    expect(stripTerminalControls("\u001B]0;camera-log\u0007ready")).toBe("ready");
  });

  it("removes the literal caret representation of a complete CSI sequence", () => {
    expect(stripTerminalControls("^[[1;31mERROR^[[0m")).toBe("ERROR");
  });

  it("preserves ordinary caret text and incomplete control sequences", () => {
    expect(stripTerminalControls("keep ^[note")).toBe("keep ^[note");
    expect(stripTerminalControls("\u001B[31")).toBe("\u001B[31");
    expect(stripTerminalControls("^[[31")).toBe("^[[31");
  });

  it("does not scan without bound for an unterminated long sequence", () => {
    const long = `\u001B[${"1".repeat(80)}text`;
    expect(stripTerminalControls(long)).toBe(long);
  });

  it("preserves device text that contains private-use characters", () => {
    expect(stripTerminalControls("before \uE000123\uE001 \u001B[31mERROR")).toBe(
      "before \uE000123\uE001 ERROR",
    );
  });

  it("removes a CSI sequence split across live frames without mutating the raw row", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest({ data: btoa("status \u001B[1;"), fileId: "file", offset: 0, sessionId: "one" });
    buffer.ingest({ data: btoa("31mERROR\u001B[0m\n"), fileId: "file", offset: 11, sessionId: "one" });

    expect(buffer.lines).toEqual(["status \u001B[1;31mERROR\u001B[0m"]);
    expect(stripTerminalControls(buffer.lines[0])).toBe("status ERROR");
  });

  it("holds an incomplete CSI suffix until the next sequential file chunk", () => {
    const cleaner = new TerminalDisplayCleaner();

    expect(cleaner.append("before \u001B[1;")).toBe("before ");
    expect(cleaner.append("31mERROR\u001B[0m\n", true)).toBe("ERROR\n");
  });

  it("does not hold text following a complete CSI sequence", () => {
    const cleaner = new TerminalDisplayCleaner();

    expect(cleaner.append("before \u001B[31mERROR")).toBe("before ERROR");
  });
});
