/** 归档页面使用上海自然日，避免浏览器本地时区改变小时目录和检索范围。 */
const SHANGHAI_DATE_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

/** 返回指定时刻在上海时区对应的 YYYY-MM-DD 日期。 */
export function shanghaiToday(reference = new Date()): string {
  const values = Object.fromEntries(
    SHANGHAI_DATE_FORMATTER.formatToParts(reference)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return `${values.year}-${values.month}-${values.day}`;
}

/** 把上海自然日转换为后端检索使用的 UTC 半开区间。 */
export function shanghaiDayBounds(date: string): { start: string; end: string } {
  const start = new Date(`${date}T00:00:00+08:00`);
  const end = new Date(start.getTime() + 24 * 60 * 60 * 1000);
  return { start: start.toISOString(), end: end.toISOString() };
}
