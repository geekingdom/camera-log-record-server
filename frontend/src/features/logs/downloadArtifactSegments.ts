// 下载制品的分卷时间摘要：实际接收时间优先，旧记录退回可审计的逻辑小时。
import type { DownloadArtifactLogSegment } from "../../shared/types";

function dateTime(value?: string) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}

export function segmentTime(segment: DownloadArtifactLogSegment) {
  if (segment.firstReceivedAt || segment.lastReceivedAt)
    return `${dateTime(segment.firstReceivedAt)} 至 ${dateTime(segment.lastReceivedAt)}`;
  if (segment.hour) {
    const start = new Date(segment.hour);
    if (!Number.isNaN(start.getTime())) {
      const end = new Date(start.getTime() + 60 * 60 * 1000);
      return `逻辑小时：${dateTime(segment.hour)} 至 ${dateTime(end.toISOString())}`;
    }
    return `逻辑小时：${segment.hour}`;
  }
  return "历史分卷未保存时间";
}

export function visibleSegments(segments: DownloadArtifactLogSegment[] | undefined, expanded: boolean) {
  if (!segments) return [];
  return expanded ? segments : segments.slice(0, 2);
}
