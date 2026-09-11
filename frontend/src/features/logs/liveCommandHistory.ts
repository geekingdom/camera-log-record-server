// 手工命令历史仅属于当前登录用户和任务；调用端只在服务端确认提交成功后写入。
const HISTORY_LIMIT = 100;
const SUGGESTION_LIMIT = 12;

/** 为用户和任务建立无法彼此复用的 sessionStorage 名称。 */
export function commandHistoryKey(userId: string | undefined, taskId: string): string {
  return `camera-log.live-command-history:${encodeURIComponent(userId || "anonymous")}:${encodeURIComponent(taskId)}`;
}

/** 新提交的命令置顶、去重并截断，空白命令不进入历史。 */
export function appendCommandHistory(history: string[], command: string): string[] {
  const value = command.trim();
  if (!value) return history.slice(0, HISTORY_LIMIT);
  return [value, ...history.filter(item => item !== value)].slice(0, HISTORY_LIMIT);
}

/** 输入框候选使用字面包含匹配，避免把命令正文解释成正则。 */
export function commandSuggestions(history: string[], input: string): string[] {
  const needle = input.trim().toLocaleLowerCase();
  if (!needle) return history.slice(0, SUGGESTION_LIMIT);
  return history.filter(item => item.toLocaleLowerCase().includes(needle)).slice(0, SUGGESTION_LIMIT);
}

/** 存储不可用或被损坏时安全回退到空历史，不能借用其它用户的数据。 */
export function readCommandHistory(userId: string | undefined, taskId: string): string[] {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(commandHistoryKey(userId, taskId)) ?? "[]");
    return Array.isArray(value)
      ? value.filter((item): item is string => typeof item === "string").slice(0, HISTORY_LIMIT)
      : [];
  } catch {
    return [];
  }
}

/** 仅保存已通过 API 确认的受限历史；存储异常不影响已成功发送的命令。 */
export function writeCommandHistory(userId: string | undefined, taskId: string, history: string[]): void {
  try {
    sessionStorage.setItem(commandHistoryKey(userId, taskId), JSON.stringify(history.slice(0, HISTORY_LIMIT)));
  } catch {
    // 隐私模式或配额不足时维持当前内存历史。
  }
}
