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
