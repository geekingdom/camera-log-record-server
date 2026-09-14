// 审计游标分页仅保存当前页与已访问起始游标，筛选切换后的响应由代次隔离。
import type { CursorEventPage } from "./api";

export class EventCursorPager<T> {
  current: CursorEventPage<T> | null = null;
  starts: string[] = [];
  index = 0;
  generation = 0;
  loading = false;
  get items() {
    return this.current?.items ?? [];
  }
  get page() {
    return this.current ? this.index + 1 : 1;
  }
  get hasPrevious() {
    return this.index > 0;
  }
  get hasNext() {
    return Boolean(this.current?.hasMore && this.current.nextCursor);
  }
  async fetch(
    fetcher: (cursor: string) => Promise<CursorEventPage<T>>,
    cursor = "",
    index = 0,
  ) {
    const generation = ++this.generation;
    this.loading = true;
    try {
      const result = await fetcher(cursor);
      if (generation !== this.generation) return;
      this.current = result;
      this.starts = index ? this.starts.slice(0, index).concat(cursor) : [""];
      this.index = index;
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }
  next(fetcher: (cursor: string) => Promise<CursorEventPage<T>>) {
    return this.hasNext
      ? this.fetch(fetcher, this.current!.nextCursor!, this.index + 1)
      : Promise.resolve();
  }
  previous(fetcher: (cursor: string) => Promise<CursorEventPage<T>>) {
    return this.hasPrevious
      ? this.fetch(fetcher, this.starts[this.index - 1], this.index - 1)
      : Promise.resolve();
  }
  invalidate() {
    this.generation += 1;
    this.loading = false;
    this.current = null;
    this.starts = [];
    this.index = 0;
  }
}
