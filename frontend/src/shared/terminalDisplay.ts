// 终端控制码只在显示层移除，实时缓冲、游标和归档读取仍保留原始字节语义。
import stripAnsi from "strip-ansi";

const CARET_CSI = /\^\[\[[0-?]{0,64}[ -/]{0,16}[@-~]/g;
const INCOMPLETE_CSI = /^\u001B\[[0-?]{0,64}[ -/]{0,16}$/;
const INCOMPLETE_CARET_CSI = /^\^\[\[[0-?]{0,64}[ -/]{0,16}$/;
const PRESERVED_CSI = /\u001B\[[0-?]{65,}|\u001B\[[0-?]{0,64}[ -/]{0,16}$/g;
const MAX_CSI_LENGTH = 82;

/**
 * 去除完整且长度受限的 ANSI CSI 控制码及其字面 ^[[ 表示。
 *
 * 只匹配带最终控制字节的 CSI，普通 ^[ 文本、非法序列和超长未完成序列保持可见，
 * 避免为了渲染净化吞掉设备正文或进行无界正则扫描。
 */
export function stripTerminalControls(value: string): string {
  let result = "";
  let offset = 0;

  // strip-ansi 会移除不完整 ESC 前缀；将受限以外的片段分段保留，避免占位符与设备正文冲突。
  for (const match of value.matchAll(PRESERVED_CSI)) {
    const index = match.index ?? 0;
    result += cleanDisplaySegment(value.slice(offset, index));
    result += match[0];
    offset = index + match[0].length;
  }
  return result + cleanDisplaySegment(value.slice(offset));
}

function cleanDisplaySegment(value: string): string {
  return stripAnsi(value).replace(CARET_CSI, "");
}

/** 连续文件读取时暂存末尾不完整 CSI，避免下一页留下控制码后半段。 */
export class TerminalDisplayCleaner {
  private pending = "";

  append(value: string, complete = false): string {
    const combined = this.pending + value;
    this.pending = complete ? "" : trailingControlPrefix(combined);
    return stripTerminalControls(
      this.pending ? combined.slice(0, -this.pending.length) : combined,
    );
  }

  reset(): void {
    this.pending = "";
  }
}

function trailingControlPrefix(value: string): string {
  const escape = value.lastIndexOf("\u001B");
  if (escape >= 0) {
    const suffix = value.slice(escape);
    if (
      (suffix === "\u001B" ||
        (suffix.startsWith("\u001B[") &&
          suffix.length <= MAX_CSI_LENGTH &&
          INCOMPLETE_CSI.test(suffix)))
    )
      return suffix;
  }
  const caret = value.lastIndexOf("^[");
  if (caret >= 0) {
    const suffix = value.slice(caret);
    if (
      (suffix === "^[" ||
        (suffix.startsWith("^[[") &&
          suffix.length <= MAX_CSI_LENGTH &&
          INCOMPLETE_CARET_CSI.test(suffix)))
    )
      return suffix;
  }
  return "";
}
