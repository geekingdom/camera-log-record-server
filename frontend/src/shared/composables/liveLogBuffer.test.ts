import { describe, expect, it } from "vitest";
import { LiveLogBuffer } from "./liveLogBuffer";

const frame = (text: string, offset: number, sessionId = "one") => ({
  data: btoa(text),
  fileId: "file",
  offset,
  sessionId,
  cursor: `${sessionId}:${offset}`,
});
describe("LiveLogBuffer", () => {
  it("keeps the new suffix of a partially overlapping frame", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest(frame("abc", 0));
    buffer.ingest(frame("bcdef\n", 1));
    expect(buffer.lines).toEqual(["abcdef"]);
  });
  it("does not join partial lines across a missing byte range", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest(frame("old", 0));
    buffer.ingest(frame("new\n", 10));
    expect(buffer.lines).toEqual(["new"]);
    expect(buffer.gaps).toBe(1);
  });
  it("drops duplicate and overlapping offsets before decoding", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest(frame("first\n", 0), 0);
    buffer.ingest(frame("first\n", 0), 1);
    expect(buffer.lines).toEqual(["first"]);
  });
  it("preserves an UTF-8 character split across frames", () => {
    const value = new TextEncoder().encode("中\n");
    const buffer = new LiveLogBuffer();
    buffer.ingest(
      {
        data: btoa(String.fromCharCode(...value.slice(0, 2))),
        fileId: "file",
        offset: 0,
        sessionId: "one",
      },
      0,
    );
    buffer.ingest(
      {
        data: btoa(String.fromCharCode(...value.slice(2))),
        fileId: "file",
        offset: 2,
        sessionId: "one",
      },
      1,
    );
    expect(buffer.lines).toEqual(["中"]);
  });
  it("resets partial bytes and offsets when the session changes", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest(frame("stale", 0, "one"), 0);
    buffer.ingest(frame("fresh\n", 0, "two"), 1);
    expect(buffer.lines).toEqual(["fresh"]);
  });
  it("records server gaps without inserting untrusted event text", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest({ type: "gap", message: "<script>" });
    expect(buffer.gaps).toBe(1);
    expect(buffer.lines).toEqual([]);
  });
  it("clears only local rows while retaining the reconnect cursor and offsets", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest(frame("first\n", 0));
    buffer.clearView();
    buffer.ingest(frame("second\n", 6));
    expect(buffer.cursor).toBe("one:6");
    expect(buffer.lines).toEqual(["second"]);
  });
  it("resets every stream field when switching tasks", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest(frame("first\n", 0));
    buffer.reset();
    buffer.ingest(frame("new\n", 0, "next"));
    expect(buffer.cursor).toBe("next:0");
    expect(buffer.lines).toEqual(["new"]);
  });
});
