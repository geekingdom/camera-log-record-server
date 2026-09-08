/**
 * 统一处理会改变运行状态或持久化配置的二次确认。
 * 取消和关闭确认框属于正常用户路径，其他异常继续交给调用方报告。
 */
import { ElMessageBox } from "element-plus";

export async function confirmAction(message: string, title: string): Promise<boolean> {
  try {
    await ElMessageBox.confirm(message, title, {
      confirmButtonText: "确认",
      cancelButtonText: "取消",
      type: "warning",
    });
    return true;
  } catch (error) {
    if (error === "cancel" || error === "close") return false;
    throw error;
  }
}
