// 验证翻页失败保留与异步结果代次隔离，不依赖真实网络。
import { describe, expect, it } from "vitest";
import { EventCursorPager } from "./eventPager";

const page = (id: string, nextCursor: string | null) => ({ items: [{ id }], pageSize: 20, hasMore: Boolean(nextCursor), nextCursor, total: null });

describe("审计事件游标分页", () => {
  it("下一页失败保留当前成功页并可重试", async () => {
    const pager = new EventCursorPager<{ id: string }>();
    await pager.fetch(async () => page("first", "next"));
    await expect(pager.next(async () => { throw new Error("temporary"); })).rejects.toThrow("temporary");
    expect(pager.items).toEqual([{ id: "first" }]);
    await pager.next(async () => page("second", null));
    expect(pager.items).toEqual([{ id: "second" }]);
  });

  it("筛选失效后拒绝迟到响应", async () => {
    const pager = new EventCursorPager<{ id: string }>();
    let resolve!: (value: ReturnType<typeof page>) => void;
    const stale = pager.fetch(() => new Promise(done => { resolve = done; }));
    pager.invalidate();
    await pager.fetch(async () => page("fresh", null));
    resolve(page("stale", null));
    await stale;
    expect(pager.items).toEqual([{ id: "fresh" }]);
  });
});
