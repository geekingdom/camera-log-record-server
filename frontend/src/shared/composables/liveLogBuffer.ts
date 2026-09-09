// 实时日志纯状态机：组件只处理连接和渲染，字节拼接、去重、限流在此保持可测试。
export interface LiveLogFrame {
  data?: string;
  cursor?: string;
  fileId?: string;
  offset?: number;
  sessionId?: string;
  type?: string;
  message?: string;
}

export interface LiveLogRange {
  id: string;
  sessionId?: string;
  fileId?: string;
  start?: number;
  end?: number;
  reason: "rate" | "transport" | "retention" | "server";
  lines?: number;
  message?: string;
}

interface BytePiece {
  bytes: Uint8Array;
  sessionId?: string;
  fileId?: string;
  start?: number;
  end?: number;
}

interface StoredLine {
  text: string;
  pieces: Array<Omit<BytePiece, "bytes">>;
}

const DISPLAY_LINE_LIMIT = 200;
const RETAINED_LINE_LIMIT = 5000;
const LONG_LINE_BYTE_LIMIT = 65536;
const TRACKED_FILE_LIMIT = 200;
const MISSING_RANGE_LIMIT = 200;

export class LiveLogBuffer {
  readonly lines: string[] = [];
  readonly missingRanges: LiveLogRange[] = [];
  omitted = 0;
  gaps = 0;
  droppedRangeCount = 0;
  cursor: string | null = null;
  private partial: BytePiece[] = [];
  private partialByteLength = 0;
  private sessionId: string | undefined;
  private offsets = new Map<string, number>();
  private visibleLines: StoredLine[] = [];
  private rateStartedAt = 0;
  private linesThisSecond = 0;
  private nextRangeId = 1;

  // 任务切换时必须切断所有旧任务状态，新的订阅从空 cursor 和空字节队列开始。
  reset() {
    this.lines.splice(0);
    this.missingRanges.splice(0);
    this.omitted = 0;
    this.gaps = 0;
    this.droppedRangeCount = 0;
    this.cursor = null;
    this.partial = [];
    this.partialByteLength = 0;
    this.sessionId = undefined;
    this.offsets.clear();
    this.visibleLines = [];
    this.rateStartedAt = 0;
    this.linesThisSecond = 0;
    this.nextRangeId = 1;
  }

  // 用户“清空”只影响本地视图；保留 cursor/offset，后续帧仍能连续去重。
  clearView() {
    this.lines.splice(0);
    this.missingRanges.splice(0);
    this.omitted = 0;
    this.gaps = 0;
    this.droppedRangeCount = 0;
    this.visibleLines = [];
  }

  // offset 在解码前比较，避免重叠帧使同一字节进入行队列两次。
  ingest(frame: LiveLogFrame, now = Date.now()) {
    if (frame.type === "gap") {
      if (frame.cursor) this.cursor = frame.cursor;
      this.gaps += 1;
      // 服务端 gap 通常没有可信 offset；保留提示但绝不猜测可补读边界。
      this.addRange({ reason: "server", message: frame.message });
      this.discardPartial("server");
      return;
    }
    if (!frame.data) return;
    let bytes = Uint8Array.from(atob(frame.data), (value) =>
      value.charCodeAt(0),
    );
    const endOffset =
      frame.offset === undefined ? undefined : frame.offset + bytes.byteLength;
    if (
      frame.offset !== undefined &&
      (!Number.isSafeInteger(frame.offset) ||
        frame.offset < 0 ||
        !Number.isSafeInteger(endOffset))
    )
      throw new TypeError("日志帧 offset 必须是非负安全整数。");
    if (frame.sessionId !== this.sessionId) this.resetSession(frame.sessionId);
    const key = frame.fileId ?? "__stream__";
    const offset = frame.offset;
    const previous = this.offsets.get(key);
    let sourceStart = offset;
    if (offset !== undefined && previous !== undefined) {
      if (endOffset !== undefined && endOffset <= previous) {
        if (frame.cursor) this.cursor = frame.cursor;
        return;
      }
      if (offset < previous) {
        bytes = bytes.subarray(previous - offset);
        sourceStart = previous;
      }
      if (offset > previous) {
        this.gaps += 1;
        this.addRange({
          reason: "transport",
          sessionId: frame.sessionId,
          fileId: frame.fileId,
          start: previous,
          end: offset,
        });
        this.discardPartial("transport");
      }
    }
    if (offset !== undefined && endOffset !== undefined)
      this.rememberOffset(key, endOffset);
    this.appendBytes(
      {
        bytes,
        sessionId: frame.sessionId,
        fileId: frame.fileId,
        start: sourceStart,
        end:
          sourceStart === undefined
            ? undefined
            : sourceStart + bytes.byteLength,
      },
      now,
    );
    if (frame.cursor) this.cursor = frame.cursor;
  }

  flush(now = Date.now()) {
    if (this.partialByteLength)
      this.emitLine(this.takePartial(this.partialByteLength), now);
  }

  // 会话变化代表远端流重新开始，旧会话的半行和 file offset 都不能沿用。
  private resetSession(sessionId: string | undefined) {
    this.sessionId = sessionId;
    this.discardPartial("transport");
    this.offsets.clear();
  }

  private rememberOffset(key: string, endOffset: number) {
    this.offsets.delete(key);
    this.offsets.set(key, endOffset);
    if (this.offsets.size > TRACKED_FILE_LIMIT) {
      const oldest = this.offsets.keys().next().value;
      if (oldest !== undefined) this.offsets.delete(oldest);
    }
  }

  private appendBytes(piece: BytePiece, now: number) {
    let cursor = 0;
    while (cursor < piece.bytes.byteLength) {
      const newlineAt = piece.bytes.indexOf(10, cursor);
      const end = newlineAt === -1 ? piece.bytes.byteLength : newlineAt + 1;
      while (cursor < end) {
        const available = LONG_LINE_BYTE_LIMIT - this.partialByteLength;
        const taken = Math.min(end - cursor, available);
        this.pushPartial(this.slicePiece(piece, cursor, cursor + taken));
        cursor += taken;
        if (this.partialByteLength === LONG_LINE_BYTE_LIMIT && cursor < end)
          this.emitLine(
            this.takePartial(this.safeUtf8PrefixLength(LONG_LINE_BYTE_LIMIT)),
            now,
          );
      }
      if (newlineAt !== -1)
        this.emitLine(this.takePartial(this.partialByteLength), now);
      while (this.partialByteLength > LONG_LINE_BYTE_LIMIT)
        this.emitLine(
          this.takePartial(this.safeUtf8PrefixLength(LONG_LINE_BYTE_LIMIT)),
          now,
        );
    }
  }

  private pushPartial(piece: BytePiece) {
    if (piece.bytes.byteLength) {
      this.partial.push(piece);
      this.partialByteLength += piece.bytes.byteLength;
    }
  }

  private takePartial(length: number) {
    const result: BytePiece[] = [];
    let remaining = length;
    while (remaining > 0 && this.partial.length) {
      const piece = this.partial[0];
      const taken = Math.min(remaining, piece.bytes.byteLength);
      result.push(this.slicePiece(piece, 0, taken));
      this.partialByteLength -= taken;
      remaining -= taken;
      if (taken === piece.bytes.byteLength) this.partial.shift();
      else
        this.partial[0] = this.slicePiece(piece, taken, piece.bytes.byteLength);
    }
    return result;
  }

  private discardPartial(reason?: LiveLogRange["reason"]) {
    if (reason && this.partialByteLength)
      this.recordPieces(this.partial, reason);
    this.partial = [];
    this.partialByteLength = 0;
  }

  private slicePiece(piece: BytePiece, start: number, end: number): BytePiece {
    return {
      ...piece,
      bytes: piece.bytes.subarray(start, end),
      start: piece.start === undefined ? undefined : piece.start + start,
      end: piece.start === undefined ? undefined : piece.start + end,
    };
  }

  // 强制切长行时避开 UTF-8 continuation byte，保证下一段仍能从完整字符开始。
  private safeUtf8PrefixLength(limit: number) {
    const bytes = this.joinPieces(this.partial);
    const length = Math.min(limit, bytes.byteLength);
    let lead = length - 1;
    while (lead >= 0 && (bytes[lead] & 0xc0) === 0x80) lead -= 1;
    if (lead < 0) return length;
    const expected = this.utf8Width(bytes[lead]);
    const present = length - lead;
    return expected > present ? lead : length;
  }

  private utf8Width(byte: number) {
    if ((byte & 0x80) === 0) return 1;
    if ((byte & 0xe0) === 0xc0) return 2;
    if ((byte & 0xf0) === 0xe0) return 3;
    if ((byte & 0xf8) === 0xf0) return 4;
    return 1;
  }

  private emitLine(pieces: BytePiece[], now: number) {
    if (now - this.rateStartedAt >= 1000) {
      this.rateStartedAt = now;
      this.linesThisSecond = 0;
    }
    this.linesThisSecond += 1;
    if (this.linesThisSecond > DISPLAY_LINE_LIMIT) {
      this.omitted += 1;
      this.recordPieces(pieces, "rate", 1);
      return;
    }
    const line: StoredLine = {
      text: this.decodeLine(pieces),
      pieces: this.rangePieces(pieces),
    };
    this.lines.push(line.text);
    this.visibleLines.push(line);
    if (this.lines.length > RETAINED_LINE_LIMIT) {
      const removed = this.visibleLines.shift();
      this.lines.shift();
      if (removed) this.recordPieces(removed.pieces, "retention", 1);
    }
  }

  private decodeLine(pieces: BytePiece[]) {
    const text = new TextDecoder().decode(this.joinPieces(pieces));
    return text.endsWith("\n")
      ? text.slice(0, text.endsWith("\r\n") ? -2 : -1)
      : text;
  }

  private joinPieces(pieces: BytePiece[]) {
    const bytes = new Uint8Array(
      pieces.reduce((sum, piece) => sum + piece.bytes.byteLength, 0),
    );
    let offset = 0;
    for (const piece of pieces) {
      bytes.set(piece.bytes, offset);
      offset += piece.bytes.byteLength;
    }
    return bytes;
  }

  private rangePieces(pieces: BytePiece[]) {
    const result: Array<Omit<BytePiece, "bytes">> = [];
    for (const { bytes: _bytes, ...piece } of pieces) {
      const previous = result.at(-1);
      if (
        previous &&
        previous.sessionId === piece.sessionId &&
        previous.fileId === piece.fileId &&
        previous.end !== undefined &&
        piece.start !== undefined &&
        previous.end === piece.start
      ) {
        previous.end = piece.end;
      } else result.push(piece);
    }
    return result;
  }

  private recordPieces(
    pieces: Array<BytePiece | Omit<BytePiece, "bytes">>,
    reason: LiveLogRange["reason"],
    lines?: number,
  ) {
    for (const piece of pieces) {
      if (piece.start === undefined || piece.end === undefined) continue;
      this.addRange({
        reason,
        sessionId: piece.sessionId,
        fileId: piece.fileId,
        start: piece.start,
        end: piece.end,
        lines: pieces.length === 1 ? lines : undefined,
      });
    }
  }

  // 仅同来源且实际相邻/重叠的缺失段合并，已有范围的 id 在合并后保持稳定。
  private addRange(range: Omit<LiveLogRange, "id">) {
    const compatible = this.missingRanges.filter(
      (candidate) =>
        candidate.reason === range.reason &&
        candidate.sessionId === range.sessionId &&
        candidate.fileId === range.fileId &&
        candidate.start !== undefined &&
        candidate.end !== undefined &&
        range.start !== undefined &&
        range.end !== undefined &&
        candidate.start <= range.end &&
        range.start <= candidate.end,
    );
    if (compatible.length) {
      const first = compatible[0];
      first.start = Math.min(first.start!, range.start!);
      first.end = Math.max(first.end!, range.end!);
      first.lines =
        first.lines === undefined || range.lines === undefined
          ? undefined
          : first.lines + range.lines;
      for (const candidate of compatible.slice(1)) {
        first.start = Math.min(first.start!, candidate.start!);
        first.end = Math.max(first.end!, candidate.end!);
        first.lines =
          first.lines === undefined || candidate.lines === undefined
            ? undefined
            : first.lines + candidate.lines;
        this.missingRanges.splice(this.missingRanges.indexOf(candidate), 1);
      }
      this.missingRanges.splice(this.missingRanges.indexOf(first), 1);
      this.missingRanges.push(first);
      return;
    }
    this.missingRanges.push({ id: String(this.nextRangeId++), ...range });
    if (this.missingRanges.length > MISSING_RANGE_LIMIT) {
      this.missingRanges.shift();
      this.droppedRangeCount += 1;
    }
  }
}
