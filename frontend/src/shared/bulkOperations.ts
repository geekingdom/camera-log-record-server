// 批量操作统一按用户选择顺序串行执行，避免并发请求掩盖版本或所有权冲突。
export interface BulkOperationItem {
  id: string;
  name: string;
  version?: number;
}

export interface BulkOperationEntry {
  id: string;
  name: string;
  status: "success" | "error" | "skipped";
  error?: string;
}

export interface BulkOperationOptions {
  onProgress?: (entries: BulkOperationEntry[]) => void;
  shouldContinue?: () => boolean;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "操作失败";
}

/**
 * 每次发起下一项请求前检查 shouldContinue。页面切换、登出或组件卸载后，尚未开始的
 * 项目统一标记 skipped；已开始的请求不伪造取消或重试，结果仍按实际响应记录。
 */
export async function runSequentially(
  items: BulkOperationItem[],
  operation: (id: string, version: number) => Promise<void>,
  options: BulkOperationOptions = {},
): Promise<BulkOperationEntry[]> {
  const entries: BulkOperationEntry[] = [];
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    if (options.shouldContinue && !options.shouldContinue()) {
      for (const skipped of items.slice(index)) entries.push({ id: skipped.id, name: skipped.name, status: "skipped" });
      options.onProgress?.([...entries]);
      break;
    }
    try {
      await operation(item.id, item.version ?? 1);
      entries.push({ id: item.id, name: item.name, status: "success" });
    } catch (error) {
      entries.push({ id: item.id, name: item.name, status: "error", error: errorMessage(error) });
    }
    options.onProgress?.([...entries]);
  }
  return entries;
}
