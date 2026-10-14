// 资源看板的展示换算保持纯函数，避免把字节数或空数据误解为实时业务结论。
import type { DashboardDailyMetric } from "../../shared/types";

/** 上海自然日用于默认查询和日期控件，避免浏览器所在地时区改变统计日。 */
export function shanghaiDate(now = new Date()) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(now);
}

/** 存储统计保留原始字节，界面仅在展示边界进行紧凑换算。 */
export function formatDashboardBytes(value: number | null | undefined) {
  if (!Number.isFinite(value) || Number(value) < 0) return "暂无数据";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = Number(value), index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount >= 100 || index === 0 ? Math.round(amount) : amount.toFixed(1)} ${units[index]}`;
}

/** 缺日仍保留服务端顺序，图表不通过补零制造不存在的采集量。 */
export function trendPoints(daily: DashboardDailyMetric[]) {
  return daily.map(item => ({ date: item.date, bytes: Number.isFinite(item.bytes) ? item.bytes : null }));
}

export function nodeHealthLabel(status: string) {
  return ({ HEALTHY: "健康", WARNING: "需关注", CRITICAL: "异常", OFFLINE: "离线" } as Record<string, string>)[status] ?? (status || "未知");
}
