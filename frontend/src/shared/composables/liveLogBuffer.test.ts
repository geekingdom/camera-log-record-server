import { describe, expect, it } from "vitest";
import { LiveLogBuffer } from "./liveLogBuffer";

const frame = (text: string, offset: number, sessionId = "one") => ({
  data: btoa(text),
  fileId: "file",
  offset,
  sessionId,
  cursor: `${sessionId}:${offset}`,
});
const binaryFrame = (text: string, offset: number, sessionId = "one") => {
  const bytes = new TextEncoder().encode(text);
  return frame(String.fromCharCode(...bytes), offset, sessionId);
};
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
  it("keeps an UTF-8 character intact when a long line reaches the byte cap", () => {
    const buffer = new LiveLogBuffer();
    const first = `${"a".repeat(65535)}中`;
    buffer.ingest(binaryFrame(first, 0), 0);
    buffer.ingest(
      binaryFrame("\n", new TextEncoder().encode(first).byteLength),
      1,
    );

    expect(buffer.lines).toEqual(["a".repeat(65535), "中"]);
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
    expect(buffer.missingRanges).toEqual([
      expect.objectContaining({ reason: "server", message: "<script>" }),
    ]);
    expect(buffer.missingRanges[0]).not.toHaveProperty("start");
    expect(buffer.missingRanges[0]).not.toHaveProperty("end");
  });
  it("rejects an invalid offset before it can reset a continuous session", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest(frame("old", 0));

    expect(() =>
      buffer.ingest({
        data: btoa("wrong"),
        cursor: "bad-offset",
        fileId: "file",
        offset: -1,
        sessionId: "two",
      }),
    ).toThrow("offset");
    buffer.ingest(frame("\n", 3));

    expect(buffer.lines).toEqual(["old"]);
    expect(buffer.cursor).toBe("one:3");
  });
  it("does not advance the reconnect cursor for malformed data", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest(frame("first\n", 0));

    expect(() =>
      buffer.ingest({ data: "not base64!", cursor: "bad" }),
    ).toThrow();
    expect(buffer.cursor).toBe("one:0");
  });
  it("cuts a received partial line at a server gap and preserves its known bytes", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest(frame("old", 0));
    buffer.ingest({ type: "gap", message: "unavailable" });
    buffer.ingest(frame("new\n", 3));

    expect(buffer.lines).toEqual(["new"]);
    expect(buffer.missingRanges).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          reason: "server",
          fileId: "file",
          start: 0,
          end: 3,
        }),
        expect.objectContaining({ reason: "server", message: "unavailable" }),
      ]),
    );
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

  it("records rate-limited UTF-8 lines with their original byte ranges", () => {
    const buffer = new LiveLogBuffer();
    const text = `${"a\n".repeat(200)}中\n`;
    buffer.ingest(binaryFrame(text, 0), 0);
    const start = new TextEncoder().encode("a\n".repeat(200)).byteLength;

    expect(buffer.lines).toHaveLength(200);
    expect(buffer.omitted).toBe(1);
    expect(buffer.missingRanges).toEqual([
      expect.objectContaining({
        reason: "rate",
        sessionId: "one",
        fileId: "file",
        start,
        end: start + new TextEncoder().encode("中\n").byteLength,
        lines: 1,
      }),
    ]);
  });

  it("retains a partial line across file boundaries and records evicted bytes", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest(
      { data: btoa("before"), fileId: "first", offset: 0, sessionId: "one" },
      0,
    );
    buffer.ingest(
      { data: btoa(" after\n"), fileId: "second", offset: 0, sessionId: "one" },
      0,
    );
    let offset = 7;
    for (let index = 0; index < 5000; index += 1) {
      const text = `row-${index}\n`;
      buffer.ingest(
        { data: btoa(text), fileId: "second", offset, sessionId: "one" },
        1000 * (index + 1),
      );
      offset += text.length;
    }

    expect(buffer.lines).toHaveLength(5000);
    expect(buffer.lines[0]).toBe("row-0");
    expect(buffer.missingRanges).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          reason: "retention",
          fileId: "first",
          start: 0,
          end: 6,
        }),
        expect.objectContaining({
          reason: "retention",
          fileId: "second",
          start: 0,
          end: 7,
        }),
      ]),
    );
  });

  it("coalesces a fragmented line into one retention range", () => {
    const buffer = new LiveLogBuffer();
    for (let index = 0; index < 300; index += 1)
      buffer.ingest(frame("x", index), index * 1000);
    buffer.ingest(frame("\n", 300), 301000);
    // 内存不变量：分包数量不能变成保留行的元数据数量，也不能持有整帧后备字节。
    const retained = (buffer as unknown as {
      visibleLines: { pieces: { bytes?: Uint8Array; start: number; end: number }[] }[];
    }).visibleLines[0].pieces;
    expect(retained).toHaveLength(1);
    expect(retained[0]).toMatchObject({ start: 0, end: 301 });
    expect(retained[0].bytes).toBeUndefined();
    let offset = 301;
    for (let index = 0; index < 5000; index += 1) {
      const text = `row-${index}\n`;
      buffer.ingest(frame(text, offset), (index + 302) * 1000);
      offset += text.length;
    }

    expect(
      buffer.missingRanges.filter(
        (range) => range.reason === "retention" && range.start === 0,
      ),
    ).toHaveLength(1);
  });

  it("does not merge omissions across bytes that stayed visible", () => {
    const buffer = new LiveLogBuffer();
    buffer.ingest(frame(`${"a\n".repeat(201)}`, 0), 0);
    buffer.ingest(frame("visible\n", 402), 1000);
    buffer.ingest(frame(`${"b\n".repeat(201)}`, 410), 1000);

    expect(
      buffer.missingRanges.filter((range) => range.reason === "rate"),
    ).toHaveLength(2);
  });

  it("bounds retained missing ranges while preserving the dropped-range count", () => {
    const buffer = new LiveLogBuffer();
    for (let index = 0; index < 201; index += 1)
      buffer.ingest({ type: "gap", message: `gap-${index}` });

    expect(buffer.missingRanges).toHaveLength(200);
    expect(buffer.droppedRangeCount).toBe(1);
  });
  it("refreshes a merged range so the range cap evicts the least recent entry", () => {
    const buffer = new LiveLogBuffer();
    const addRange = (
      buffer as unknown as {
        addRange: (range: {
          reason: "transport";
          fileId: string;
          start: number;
          end: number;
        }) => void;
      }
    ).addRange.bind(buffer);
    for (let index = 0; index < 200; index += 1)
      addRange({
        reason: "transport",
        fileId: `file-${index}`,
        start: 0,
        end: 1,
      });
    addRange({ reason: "transport", fileId: "file-0", start: 1, end: 2 });
    addRange({ reason: "transport", fileId: "new", start: 0, end: 1 });

    expect(buffer.missingRanges).toHaveLength(200);
    expect(
      buffer.missingRanges.some((range) => range.fileId === "file-0"),
    ).toBe(true);
    expect(
      buffer.missingRanges.some((range) => range.fileId === "file-1"),
    ).toBe(false);
  });
});
