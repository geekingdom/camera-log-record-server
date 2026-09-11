// 实时与归档日志共用的纯展示切分：不改写原始日志，只为安全 span 渲染提供边界。
import { stripTerminalControls } from "../../shared/terminalDisplay";

export type LogTone = "error" | "warning" | "info" | "debug" | "trace";

export interface LogTextToken {
  text: string;
  tone?: LogTone;
  highlighted: boolean;
}

interface TextRange {
  start: number;
  end: number;
}

export const MAX_TEXT_MATCHES = 1000;
const LEVEL_PATTERN = /\b(ERROR|ERR|FATAL|WARN(?:ING)?|INFO|DEBUG|TRACE)\b/gi;

/** 返回大小写无关的字面查询命中，限制数量防止单行高频文本撑大渲染开销。 */
export function findTextMatches(text: string, query?: string, limit = MAX_TEXT_MATCHES): TextRange[] {
  const needle = query;
  if (!needle) return [];
  // 正则只做字面匹配，避免 Unicode 转小写改变长度而破坏原文位置。
  const pattern = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu");
  const matches: TextRange[] = [];
  for (const match of text.matchAll(pattern)) {
    if (matches.length >= limit) break;
    matches.push({ start: match.index, end: match.index + match[0].length });
  }
  return matches;
}

/** 将已净化的展示文本切成严重级别与搜索高亮均不重叠的安全片段。 */
export function tokenizeLogText(text: string, query?: string, activeStart?: number): LogTextToken[] {
  const display = stripTerminalControls(text);
  if (!display) return [];
  const highlights = findTextMatches(display, query);
  if (query && activeStart !== undefined && !highlights.some(item => item.start === activeStart)) {
    const active = findTextMatches(display.slice(activeStart), query, 1)[0];
    if (active?.start === 0) highlights.push({ start: activeStart, end: activeStart + active.end });
    highlights.sort((left, right) => left.start - right.start);
  }
  const levels: Array<TextRange & { tone: LogTone }> = [];
  for (const match of display.matchAll(LEVEL_PATTERN)) {
    const value = match[0].toUpperCase();
    levels.push({
      start: match.index ?? 0,
      end: (match.index ?? 0) + match[0].length,
      tone: value === "ERROR" || value === "ERR" || value === "FATAL"
        ? "error"
        : value === "WARN" || value === "WARNING"
          ? "warning"
          : value === "INFO"
            ? "info"
            : value === "DEBUG"
              ? "debug"
              : "trace",
    });
  }
  const edges = new Set<number>([0, display.length]);
  for (const range of [...highlights, ...levels]) {
    edges.add(range.start);
    edges.add(range.end);
  }
  const ordered = [...edges].sort((left, right) => left - right);
  const tokens: LogTextToken[] = [];
  let levelIndex = 0;
  let highlightIndex = 0;
  for (let index = 0; index < ordered.length - 1; index += 1) {
    const start = ordered[index];
    const end = ordered[index + 1];
    if (start === end) continue;
    while (levels[levelIndex] && levels[levelIndex].end <= start) levelIndex += 1;
    while (highlights[highlightIndex] && highlights[highlightIndex].end <= start) highlightIndex += 1;
    tokens.push({
      text: display.slice(start, end),
      tone: levels[levelIndex]?.start <= start && levels[levelIndex]?.end >= end ? levels[levelIndex].tone : undefined,
      highlighted: highlights[highlightIndex]?.start <= start && highlights[highlightIndex]?.end >= end,
    });
  }
  return tokens;
}
