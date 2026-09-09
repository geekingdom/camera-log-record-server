// 命令草稿删除确认：确认等待期间只删除用户最初选择的同一对象，绝不按过期下标误删。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ref } from "vue";

vi.mock("../../shared/confirm", () => ({ confirmAction: vi.fn() }));

import { confirmAction } from "../../shared/confirm";
import { confirmAndRemoveItem } from "./removeCommand";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.resetAllMocks();
});

describe("confirmAndRemoveItem", () => {
  it("取消确认时保持命令列表不变", async () => {
    const first = { command: "logread" };
    const items = ref([first]);
    vi.mocked(confirmAction).mockResolvedValue(false);

    await expect(confirmAndRemoveItem(items, first, "删除？", "确认")).resolves.toBe(false);

    expect(items.value).toEqual([first]);
  });

  it("同文本命令只删除被点击的对象", async () => {
    const first = { command: "logread" };
    const second = { command: "logread" };
    const items = ref([first, second]);
    vi.mocked(confirmAction).mockResolvedValue(true);

    await expect(confirmAndRemoveItem(items, second, "删除？", "确认")).resolves.toBe(true);

    expect(items.value).toEqual([first]);
  });

  it("确认期间替换为新命令时不删除新内容", async () => {
    const original = { command: "logread" };
    const replacement = { command: "logread" };
    const items = ref([original]);
    const confirmation = deferred<boolean>();
    vi.mocked(confirmAction).mockReturnValue(confirmation.promise);

    const removing = confirmAndRemoveItem(items, original, "删除？", "确认");
    items.value = [replacement];
    confirmation.resolve(true);

    await expect(removing).resolves.toBe(false);
    expect(items.value).toEqual([replacement]);
  });

  it("确认期间重排后仍删除被点击对象而非旧下标", async () => {
    const first = { command: "first" };
    const second = { command: "second" };
    const third = { command: "third" };
    const items = ref([first, second, third]);
    const confirmation = deferred<boolean>();
    vi.mocked(confirmAction).mockReturnValue(confirmation.promise);

    const removing = confirmAndRemoveItem(items, second, "删除？", "确认");
    items.value = [third, first, second];
    confirmation.resolve(true);

    await expect(removing).resolves.toBe(true);
    expect(items.value).toEqual([third, first]);
  });
});
