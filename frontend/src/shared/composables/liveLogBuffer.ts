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

export class LiveLogBuffer {
  readonly lines: string[] = [];
  omitted = 0;
  gaps = 0;
  cursor: string | null = null;
  private decoder = new TextDecoder();
  private partial = "";
  private sessionId: string | undefined;
  private offsets = new Map<string, number>();
  private rateStartedAt = 0;
  private linesThisSecond = 0;

  // 任务切换时必须切断所有旧任务状态，新的订阅从空 cursor 和空 decoder 开始。
  reset() {
    this.lines.splice(0);
    this.omitted = 0;
    this.gaps = 0;
    this.cursor = null;
    this.decoder = new TextDecoder();
    this.partial = "";
    this.sessionId = undefined;
    this.offsets.clear();
    this.rateStartedAt = 0;
    this.linesThisSecond = 0;
  }

  // 用户“清空”只影响本地视图；保留 cursor/offset，后续帧仍能连续去重。
  clearView() {
    this.lines.splice(0);
    this.omitted = 0;
    this.gaps = 0;
  }

  // offset 在解码前比较，避免重复帧重复推进 UTF-8 decoder 的内部状态。
  ingest(frame: LiveLogFrame, now = Date.now()) {
    if (frame.cursor) this.cursor = frame.cursor;
    if (frame.type === "gap") {
      this.gaps += 1;
      return;
    }
    if (!frame.data) return;
    if (frame.sessionId !== this.sessionId) this.resetSession(frame.sessionId);
    const key = frame.fileId ?? "__stream__";
    const offset = frame.offset ?? -1;
    const previous = this.offsets.get(key);
    let bytes = Uint8Array.from(atob(frame.data), (value) =>
      value.charCodeAt(0),
    );
    const endOffset = offset + bytes.byteLength;
    if (offset >= 0 && previous !== undefined) {
      if (endOffset <= previous) return;
      if (offset < previous) bytes = bytes.subarray(previous - offset);
      if (offset > previous) {
        this.gaps += 1;
        this.decoder = new TextDecoder();
        this.partial = "";
      }
    }
    const decoded = this.decoder.decode(bytes, { stream: true });
    if (offset >= 0) this.offsets.set(key, endOffset);
    this.append(`${this.partial}${decoded}`, now);
  }

  flush(now = Date.now()) {
    if (this.partial) {
      this.append(`${this.partial}\n`, now);
      this.partial = "";
    }
  }

  // 会话变化代表远端流重新开始，旧会话的半个字符和 offset 都不能沿用。
  private resetSession(sessionId: string | undefined) {
    this.sessionId = sessionId;
    this.decoder = new TextDecoder();
    this.partial = "";
    this.offsets.clear();
  }

  private append(text: string, now: number) {
    const parts = text.split(/\r?\n/);
    this.partial = parts.pop() ?? "";
    while (this.partial.length > 65536) {
      parts.push(this.partial.slice(0, 65536));
      this.partial = this.partial.slice(65536);
    }
    if (now - this.rateStartedAt >= 1000) {
      this.rateStartedAt = now;
      this.linesThisSecond = 0;
    }
    const available = Math.max(0, 200 - this.linesThisSecond);
    const accepted = parts.slice(0, available);
    this.linesThisSecond += parts.length;
    this.omitted += parts.length - accepted.length;
    this.lines.push(...accepted);
    if (this.lines.length > 5000)
      this.lines.splice(0, this.lines.length - 5000);
  }
}
