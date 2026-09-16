// 实时日志纯状态机：组件只处理连接和渲染，字节拼接、去重和内容预算在此保持可测试。
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
  reason: "transport" | "retention" | "server";
  lines?: number;
  message?: string;
  // 服务端未知 gap 以两端已确认帧为锚点，目录接口据此跨文件定位，绝不猜测中间字节。
  beforeFileId?: string;
  beforeOffset?: number;
  beforeSessionId?: string;
  afterFileId?: string;
  afterOffset?: number;
  afterSessionId?: string;
}

interface LogAnchor { fileId: string; offset: number; sessionId?: string; }

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
  byteLength: number;
}

export const DEFAULT_LIVE_LOG_BUFFER_BYTES = 10 * 1024 * 1024;
const LONG_LINE_BYTE_LIMIT = 65536;
const TRACKED_FILE_LIMIT = 200;
const MISSING_RANGE_LIMIT = 200;
const LINE_METADATA_BYTES = 64;
const PIECE_METADATA_BYTES = 48;

export class LiveLogBuffer {
  private readonly lineTexts: Array<string | undefined> = [];
  // 读取时生成当前快照，组件本来就在 100ms 节流点复制；淘汰槽会立即清空正文引用。
  get lines() {
    return this.lineTexts.slice(this.lineStart).filter(
      (line): line is string => line !== undefined,
    );
  }
  readonly missingRanges: LiveLogRange[] = [];
  omitted = 0;
  gaps = 0;
  droppedRangeCount = 0;
  cursor: string | null = null;
  private partial: BytePiece[] = [];
  private partialByteLength = 0;
  private sessionId: string | undefined;
  private offsets = new Map<string, number>();
  private visibleLines: Array<StoredLine | undefined> = [];
  private lineStart = 0;
  private retainedByteLength = 0;
  private nextRangeId = 1;
  private lastAnchor: LogAnchor | undefined;
  private pendingServerGap: LiveLogRange | undefined;

  private contentBudgetBytes: number;

  constructor(contentBudgetBytes = DEFAULT_LIVE_LOG_BUFFER_BYTES) {
    this.contentBudgetBytes = this.validateContentBudget(contentBudgetBytes);
  }

  // 配置热更新只收缩本地行队列，绝不重置 cursor、offset 或正在接收的半行。
  setContentBudgetBytes(contentBudgetBytes: number) {
    this.contentBudgetBytes = this.validateContentBudget(contentBudgetBytes);
    return this.enforceContentBudget();
  }

  // 任务切换时必须切断所有旧任务状态，新的订阅从空 cursor 和空字节队列开始。
  reset() {
    this.lineTexts.splice(0);
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
    this.lineStart = 0;
    this.retainedByteLength = 0;
    this.nextRangeId = 1;
    this.lastAnchor = undefined;
    this.pendingServerGap = undefined;
  }

  // 用户“清空”只影响本地视图；保留 cursor/offset，后续帧仍能连续去重。
  clearView() {
    this.lineTexts.splice(0);
    this.missingRanges.splice(0);
    this.omitted = 0;
    this.gaps = 0;
    this.droppedRangeCount = 0;
    this.visibleLines = [];
    this.lineStart = 0;
    this.retainedByteLength = 0;
  }

  // offset 在解码前比较，避免重叠帧使同一字节进入行队列两次。
  ingest(frame: LiveLogFrame, now = Date.now()) {
    if (frame.type === "gap") {
      if (frame.cursor) this.cursor = frame.cursor;
      this.gaps += 1;
      // gap 本身没有位置，只有前后已接收帧共同构成可核验的目录查询锚点。
      this.pendingServerGap = this.addRange({
        reason: "server", message: frame.message,
        beforeFileId: this.lastAnchor?.fileId,
        beforeOffset: this.lastAnchor?.offset,
        beforeSessionId: this.lastAnchor?.sessionId,
      });
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
    if (frame.fileId && sourceStart !== undefined) {
      const received = { fileId: frame.fileId, offset: sourceStart + bytes.byteLength, sessionId: frame.sessionId };
      if (this.pendingServerGap) {
        this.pendingServerGap.afterFileId = frame.fileId;
        this.pendingServerGap.afterOffset = sourceStart;
        this.pendingServerGap.afterSessionId = frame.sessionId;
        this.pendingServerGap = undefined;
      }
      this.lastAnchor = received;
    }
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
    this.lastAnchor = undefined;
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

  private emitLine(pieces: BytePiece[], _now: number) {
    const text = this.decodeLine(pieces);
    const rangePieces = this.rangePieces(pieces);
    const line: StoredLine = {
      text,
      pieces: rangePieces,
      // 内容预算估算 UTF-16 字符串、数组槽和区间对象，不承诺等同浏览器进程 RSS。
      byteLength: this.estimateStoredLineBytes(text, rangePieces.length),
    };
    this.lineTexts.push(line.text);
    this.visibleLines.push(line);
    this.retainedByteLength += line.byteLength;
    // 内容预算按已解码行所对应的原始字节计量；虚拟化只影响 DOM，不能阻断接收。
    this.enforceContentBudget();
  }

  private validateContentBudget(contentBudgetBytes: number) {
    if (!Number.isSafeInteger(contentBudgetBytes) || contentBudgetBytes < 1)
      throw new TypeError("实时日志内容预算必须是正整数。");
    return contentBudgetBytes;
  }

  private estimateStoredLineBytes(text: string, rangePieceCount: number) {
    return LINE_METADATA_BYTES + text.length * 2 + rangePieceCount * PIECE_METADATA_BYTES;
  }

  private enforceContentBudget() {
    let evicted = false;
    while (this.retainedByteLength > this.contentBudgetBytes && this.lineStart < this.visibleLines.length) {
      const removed = this.visibleLines[this.lineStart];
      this.visibleLines[this.lineStart] = undefined;
      this.lineTexts[this.lineStart] = undefined;
      this.lineStart += 1;
      if (removed) {
        this.retainedByteLength -= removed.byteLength;
        this.omitted += 1;
        evicted = true;
        this.recordPieces(removed.pieces, "retention", 1);
      }
    }
    if (this.lineStart >= 1024 && this.lineStart * 2 >= this.visibleLines.length) {
      this.visibleLines.splice(0, this.lineStart);
      this.lineTexts.splice(0, this.lineStart);
      this.lineStart = 0;
    }
    return evicted;
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
  private addRange(range: Omit<LiveLogRange, "id">): LiveLogRange {
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
      return first;
    }
    const added = { id: String(this.nextRangeId++), ...range };
    this.missingRanges.push(added);
    if (this.missingRanges.length > MISSING_RANGE_LIMIT) {
      this.missingRanges.shift();
      this.droppedRangeCount += 1;
    }
    return added;
  }
}
