/** 归档页面使用上海自然日，避免浏览器本地时区改变小时目录和检索范围。 */
const SHANGHAI_DATE_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

const SHANGHAI_DATE_TIME_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
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

/** 将上海时区的 datetime-local 文本明确转换为后端要求的 UTC ISO 时间。 */
export function shanghaiDateTimeToUtc(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) throw new RangeError("上海时间必须精确到分钟");
  const [, yearText, monthText, dayText, hourText, minuteText] = match;
  const [year, month, day, hour, minute] = [yearText, monthText, dayText, hourText, minuteText].map(Number);
  const local = new Date(Date.UTC(year, month - 1, day, hour, minute));
  if (
    local.getUTCFullYear() !== year || local.getUTCMonth() !== month - 1 || local.getUTCDate() !== day
    || local.getUTCHours() !== hour || local.getUTCMinutes() !== minute
  ) throw new RangeError("上海时间无效");
  return new Date(local.getTime() - 8 * 60 * 60 * 1000).toISOString();
}

/** 默认检索最近一小时，使用上海日期时间文本避免浏览器本地时区改写控件值。 */
export function shanghaiDateTimeRange(reference = new Date()): { start: string; end: string } {
  const format = (value: Date) => {
    const parts = Object.fromEntries(SHANGHAI_DATE_TIME_FORMATTER.formatToParts(value)
      .filter(part => part.type !== "literal")
      .map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
  };
  const end = new Date(reference.getTime() - reference.getSeconds() * 1000 - reference.getMilliseconds());
  return { start: format(new Date(end.getTime() - 60 * 60 * 1000)), end: format(end) };
}
