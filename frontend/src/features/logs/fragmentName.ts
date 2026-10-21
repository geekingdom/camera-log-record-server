/** 统一展示日志片段名；归档成员名已由服务端按实际服务器接收时间冻结。 */
import type { LogFile } from "../../shared/types";

type FragmentNameFields = Pick<LogFile, "id" | "archiveMember" | "rawFileName" | "archiveName" | "firstReceivedAt" | "lastReceivedAt">;

const SHANGHAI_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});
const PREALLOCATED_NAME = /^(.*)-\d{14}-\d{14}(-part-\d+\.log)$/;
const ABSOLUTE_ISO_TIME = /(?:Z|[+-]\d{2}:\d{2})$/;

function shanghaiStamp(value?: string): string | undefined {
  if (!value || !ABSOLUTE_ISO_TIME.test(value)) return undefined;
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return undefined;
  const parts = Object.fromEntries(SHANGHAI_FORMATTER.formatToParts(instant)
    .filter((part) => part.type !== "literal")
    .map((part) => [part.type, part.value]));
  if (!parts.year || !parts.month || !parts.day || !parts.hour || !parts.minute || !parts.second) return undefined;
  return `${parts.year}${parts.month}${parts.day}${parts.hour}${parts.minute}${parts.second}`;
}

/** 为 OPEN 旧目录使用同一命名规则派生当前范围，不改变其原始文件路径或标识。 */
function currentRangeName(file: FragmentNameFields): string | undefined {
  const match = file.rawFileName?.match(PREALLOCATED_NAME);
  const first = shanghaiStamp(file.firstReceivedAt);
  const last = shanghaiStamp(file.lastReceivedAt);
  return match && first && last ? `${match[1]}-${first}-${last}${match[2]}` : undefined;
}

/** READY 优先使用已冻结的 archiveMember；历史目录保持可读回退。 */
export function displayLogFragmentName(file: FragmentNameFields): string {
  return file.archiveMember || currentRangeName(file) || file.rawFileName || file.archiveName || file.id;
}
