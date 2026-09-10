// 源文件观测状态独立于下载快照状态，未创建快照不代表设备仍在传输。
import type { CoredumpFile } from "../../shared/types";

/** 优先展示正在进行的副本操作，再解释源文件的观测结果。 */
export function coredumpFileStatus(file: CoredumpFile): string {
  const operations: Record<string, string> = {
    FREEZING: "正在准备副本", FROZEN: "副本可下载",
    RETIRING: "等待读取结束", DELETING: "正在清理副本",
  };
  if (operations[file.status]) return operations[file.status]!;
  if (file.sourceState === "STABLE") return "文件已稳定";
  if (file.sourceState === "CHANGING") return "文件更新中";
  return file.status === "RECEIVING" ? "等待稳定确认" : file.status;
}
