// 命令草稿删除的确认与稳定定位：异步确认期间，列表可能被排序或模板内容替换。
import type { Ref } from "vue";
import { confirmAction } from "../../shared/confirm";

/**
 * 确认后按对象引用从当前列表移除条目。
 *
 * 不能保留点击时的下标，因为确认框打开期间用户可调整顺序或替换模板。若原对象
 * 已不在当前数组，则放弃删除，避免旧操作侵入新的表单内容。
 */
export async function confirmAndRemoveItem<T>(
  items: Ref<T[]>,
  item: T,
  message: string,
  title: string,
): Promise<boolean> {
  if (!(await confirmAction(message, title))) return false;

  const position = items.value.indexOf(item);
  if (position < 0) return false;

  items.value = [
    ...items.value.slice(0, position),
    ...items.value.slice(position + 1),
  ];
  return true;
}
