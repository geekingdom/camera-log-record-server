import { describe, expect, it } from "vitest";

import { AuthenticationRecordCursorPager } from "./authenticationRecordPager";

const page = (label: string, nextCursor: string | null, hasMore = Boolean(nextCursor)) => ({
  items: [{ id: label, result: "SUCCESS" }],
  total: null,
  pageSize: 20,
  hasMore,
  nextCursor,
});

describe("认证记录游标分页", () => {
  it("首屏以空 cursor 请求，下一页仅在请求成功后推进", async () => {
    const pager = new AuthenticationRecordCursorPager();
    const cursors: string[] = [];
    const fetchPage = async (cursor: string) => {
      cursors.push(cursor);
      return cursor ? page("second", null) : page("first", "next-page");
    };

    await pager.refresh(fetchPage);
    expect(cursors).toEqual([""]);
    expect(pager.currentPage).toBe(1);
    expect(pager.items.map(item => item.id)).toEqual(["first"]);

    await pager.next(fetchPage);
    expect(cursors).toEqual(["", "next-page"]);
    expect(pager.currentPage).toBe(2);
    expect(pager.items.map(item => item.id)).toEqual(["second"]);
  });

  it("失败不推进页面且同一下一页可重试", async () => {
    const pager = new AuthenticationRecordCursorPager();
    const attempted: string[] = [];
    await pager.refresh(async cursor => page(cursor || "first", "next-page"));

    await expect(pager.next(async cursor => {
      attempted.push(cursor);
      throw new Error("temporary failure");
    })).rejects.toThrow("temporary failure");
    expect(pager.currentPage).toBe(1);
    expect(pager.items.map(item => item.id)).toEqual(["first"]);

    await pager.next(async cursor => {
      attempted.push(cursor);
      return page("second", null);
    });
    expect(attempted).toEqual(["next-page", "next-page"]);
    expect(pager.currentPage).toBe(2);
  });

  it("刷新使迟到的旧请求失效，且成功后回到首屏", async () => {
    const pager = new AuthenticationRecordCursorPager();
    let resolveOld: ((value: ReturnType<typeof page>) => void) | undefined;
    const old = pager.refresh(() => new Promise(resolve => { resolveOld = resolve; }));
    const current = pager.refresh(async () => page("fresh", null));

    await current;
    resolveOld?.(page("stale", null));
    await old;
    expect(pager.currentPage).toBe(1);
    expect(pager.items.map(item => item.id)).toEqual(["fresh"]);
  });

  it("上一页使用已访问的起始 cursor 重新请求，不缓存历史正文", async () => {
    const pager = new AuthenticationRecordCursorPager();
    let calls = 0;
    const fetchPage = async (cursor: string) => {
      calls += 1;
      return cursor ? page("second", null) : page("first", "next-page");
    };
    await pager.refresh(fetchPage);
    await pager.next(fetchPage);

    await pager.previous(fetchPage);
    expect(pager.currentPage).toBe(1);
    expect(pager.items.map(item => item.id)).toEqual(["first"]);
    expect(calls).toBe(3);
  });

  it("筛选失效后清空旧游标和正文，首屏失败也不会回写旧数据", async () => {
    const pager = new AuthenticationRecordCursorPager();
    await pager.refresh(async () => page("old-filter", "old-next"));
    pager.invalidate();

    await expect(pager.refresh(async () => { throw new Error("new-filter failed"); })).rejects.toThrow("new-filter failed");
    expect(pager.items).toEqual([]);
    expect(pager.currentPage).toBe(1);
    expect(pager.hasNext).toBe(false);
  });

  it("关闭或资源切换后，迟到失败不会污染当前状态", async () => {
    const pager = new AuthenticationRecordCursorPager();
    let rejectOld: ((reason: Error) => void) | undefined;
    const old = pager.refresh(() => new Promise((_resolve, reject) => { rejectOld = reject; }));
    pager.invalidate();
    await pager.refresh(async () => page("fresh", null));

    rejectOld?.(new Error("stale failure"));
    await expect(old).resolves.toBeUndefined();
    expect(pager.items.map(item => item.id)).toEqual(["fresh"]);
  });

  it("下一页请求未完成时不会重复发出，上一页失败保留当前页", async () => {
    const pager = new AuthenticationRecordCursorPager();
    await pager.refresh(async () => page("first", "next-page"));
    let resolveNext: ((value: ReturnType<typeof page>) => void) | undefined;
    const next = pager.next(() => new Promise(resolve => { resolveNext = resolve; }));
    await pager.next(async () => { throw new Error("不应重复请求"); });
    resolveNext?.(page("second", "third-page"));
    await next;
    expect(pager.currentPage).toBe(2);

    await expect(pager.previous(async () => { throw new Error("previous failed"); })).rejects.toThrow("previous failed");
    expect(pager.currentPage).toBe(2);
    expect(pager.items.map(item => item.id)).toEqual(["second"]);
  });
});
