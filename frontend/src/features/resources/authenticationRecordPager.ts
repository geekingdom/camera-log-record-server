// 认证记录游标分页：仅保存当前正文与已访问游标，隔离筛选切换后的迟到响应。
import type { CursorPage } from "../../shared/types";

type FetchPage<T> = (cursor: string) => Promise<CursorPage<T>>;

/**
 * 认证记录只保留当前页正文；上一页按已访问的起始游标重新读取，避免长期浏览占用内存。
 * 请求代次由刷新递增，资源切换或关闭时调用 invalidate() 即可拒绝旧响应。
 */
export class AuthenticationRecordCursorPager<T extends { id: string }> {
  private current: CursorPage<T> | null = null;
  private pageStarts: string[] = [];
  private index = 0;
  private generation = 0;
  private pending = false;

  get items(): T[] { return this.current?.items ?? []; }
  get currentPage(): number { return this.current ? this.index + 1 : 1; }
  get hasPrevious(): boolean { return this.index > 0; }
  get hasNext(): boolean { return Boolean(this.current?.hasMore); }
  get loading(): boolean { return this.pending; }

  /** 重新读取首屏；请求失败时保留原页面，方便用户直接重试。 */
  async refresh(fetchPage: FetchPage<T>): Promise<void> {
    const request = ++this.generation;
    this.pending = true;
    try {
      const first = await fetchPage("");
      if (request !== this.generation) return;
      this.current = first;
      this.pageStarts = [""];
      this.index = 0;
    } catch (error) {
      // 资源关闭、切换或下一次刷新后的旧失败不应显示为当前页面错误。
      if (request === this.generation) throw error;
    } finally {
      if (request === this.generation) this.pending = false;
    }
  }

  /** 仅当服务端声明存在下一页时使用当前 nextCursor 请求，成功后再推进页码。 */
  async next(fetchPage: FetchPage<T>): Promise<void> {
    const current = this.current;
    if (!current?.hasMore || !current.nextCursor || this.pending) return;
    const request = this.generation;
    this.pending = true;
    try {
      const next = await fetchPage(current.nextCursor);
      if (request !== this.generation) return;
      this.current = next;
      this.pageStarts = [...this.pageStarts.slice(0, this.index + 1), current.nextCursor];
      this.index += 1;
    } catch (error) {
      if (request === this.generation) throw error;
    } finally {
      if (request === this.generation) this.pending = false;
    }
  }

  /** 上一页按记录的起始游标重新读取；失败时保留当前页，可直接再次尝试。 */
  async previous(fetchPage: FetchPage<T>): Promise<void> {
    if (this.pending || this.index <= 0) return;
    const targetIndex = this.index - 1;
    const cursor = this.pageStarts[targetIndex];
    const request = this.generation;
    this.pending = true;
    try {
      const previous = await fetchPage(cursor);
      if (request !== this.generation) return;
      this.current = previous;
      this.pageStarts = this.pageStarts.slice(0, targetIndex + 1);
      this.index = targetIndex;
    } catch (error) {
      if (request === this.generation) throw error;
    } finally {
      if (request === this.generation) this.pending = false;
    }
  }

  /** 资源关闭或更换时使所有在途请求失效，并清空旧资源的可见数据。 */
  invalidate(): void {
    this.generation += 1;
    this.pending = false;
    this.current = null;
    this.pageStarts = [];
    this.index = 0;
  }
}
