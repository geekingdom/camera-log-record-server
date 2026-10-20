/** 工作区列表快照必须服从当前筛选与会话，迟到请求不能覆盖新视图。 */
import { nextTick, ref } from "vue";
import { describe, expect, it } from "vitest";

import type { Page, Resource, Task, Template } from "../shared/types";
import { useWorkspaceCollections } from "./useWorkspaceCollections";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
function task(id: string): Task {
  return { id, name: id, protocol: "SSH", resourceId: "resource", initialCommands: [], scheduledCommands: [] };
}
function template(id: string): Template {
  return { id, name: id, initialCommands: [], scheduledCommands: [], sharedWith: [], sharedWithAll: false };
}
const page = <T>(items: T[]): Page<T> => ({ items, total: items.length, page: 1, pageSize: 20 });

describe("useWorkspaceCollections", () => {
  it("删除筛选进入请求且切换时拒绝旧响应", async () => {
    const pending = deferred<Page<Task>>();
    const filters: Array<Record<string, string | undefined>> = [];
    const subject = useWorkspaceCollections({
      user: ref({ id: "owner", isAdmin: true }), sessionGeneration: ref(1), can: () => true,
      api: { tasks: (_page, _size, query) => { filters.push(query ?? {}); return pending.promise; },
        templates: async () => page([]), nodes: async () => page([]) }, onError: () => {},
    });
    const oldRequest = subject.loadTasks();
    subject.taskIncludeDeleted.value = true;
    pending.resolve(page([task("old")]));
    await oldRequest;
    expect(subject.tasks.value).toEqual([]);
    await subject.loadTasks();
    expect(filters[0].includeDeleted).toBeUndefined();
    expect(filters[1].includeDeleted).toBe("true");
    subject.clear();
    expect(subject.taskIncludeDeleted.value).toBe(false);
  });
  it("忽略旧筛选的任务迟到响应", async () => {
    const user = ref({ id: "user-a", isAdmin: false });
    const sessionGeneration = ref(1);
    const first = deferred<Page<Task>>();
    const second = deferred<Page<Task>>();
    let calls = 0;
    const subject = useWorkspaceCollections({
      user, sessionGeneration, can: () => true,
      api: {
        tasks: () => (++calls === 1 ? first.promise : second.promise),
        templates: async () => page([]), nodes: async () => page([]),
      },
      onError: () => {},
    });

    void subject.loadTasks();
    subject.taskSearch.value = "new";
    void subject.loadTasks();
    second.resolve(page([task("new")])) ;
    await nextTick();
    await Promise.resolve();
    first.resolve(page([task("old")]));
    await Promise.resolve();

    expect(subject.tasks.value.map(item => item.id)).toEqual(["new"]);
    expect(subject.totals.value.tasks).toBe(1);
  });

  it("筛选已变化但新请求尚未发起时不展示旧响应", async () => {
    const user = ref({ id: "user-a", isAdmin: false });
    const sessionGeneration = ref(1);
    const pending = deferred<Page<Task>>();
    const subject = useWorkspaceCollections({
      user, sessionGeneration, can: () => true,
      api: {
        tasks: () => pending.promise,
        templates: async () => page([]), nodes: async () => page([]),
      },
      onError: () => {},
    });

    void subject.loadTasks();
    subject.selectedResource.value = { id: "next-resource" } as Resource;
    pending.resolve(page([task("old-resource")]));
    await nextTick();
    await Promise.resolve();

    expect(subject.tasks.value).toEqual([]);
    expect(subject.totals.value.tasks).toBe(0);
  });

  it("排序状态进入任务请求并使迟到响应失效", async () => {
    const user = ref({ id: "user-a", isAdmin: false });
    const sessionGeneration = ref(1);
    const requests: Array<Record<string, string | undefined>> = [];
    const subject = useWorkspaceCollections({
      user, sessionGeneration, can: () => true,
      api: {
        tasks: async (_page, _size, filters) => {
          requests.push(filters ?? {});
          return page([]);
        },
        templates: async () => page([]), nodes: async () => page([]),
      },
      onError: () => {},
    });

    subject.taskSortBy.value = "name";
    subject.taskSortOrder.value = "asc";
    await subject.loadTasks();

    expect(requests).toEqual([expect.objectContaining({ sortBy: "name", sortOrder: "asc" })]);
  });

  it("忽略跨会话的模板迟到响应", async () => {
    const user = ref({ id: "user-a", isAdmin: false });
    const sessionGeneration = ref(1);
    const pending = deferred<Page<Template>>();
    const subject = useWorkspaceCollections({
      user, sessionGeneration, can: () => true,
      api: {
        tasks: async () => page([]), templates: () => pending.promise, nodes: async () => page([]),
      },
      onError: () => {},
    });

    void subject.loadTemplates();
    sessionGeneration.value = 2;
    user.value = { id: "user-b", isAdmin: false };
    pending.resolve(page([template("old-session")]));
    await Promise.resolve();

    expect(subject.templates.value).toEqual([]);
  });
});
