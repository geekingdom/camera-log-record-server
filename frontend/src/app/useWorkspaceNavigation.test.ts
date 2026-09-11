/** 工作区导航须将同步状态变化合并为一次真实刷新。 */
import { effectScope, nextTick, ref } from "vue";
import { describe, expect, it } from "vitest";

import type { Resource } from "../shared/types";
import { useWorkspaceNavigation } from "./useWorkspaceNavigation";

const resource = { id: "camera", name: "测试设备", ip: "192.0.2.1", kind: "HIKVISION_NETWORK" } as Resource;

interface SubjectOptions {
  enabled?: boolean;
  busy?: boolean;
  failSynchronously?: boolean;
  refreshOverride?: (quiet: boolean) => Promise<void> | void;
}

function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: unknown) => void;
  const promise = new Promise<T>((complete, fail) => { resolve = complete; reject = fail; });
  return { promise, reject, resolve };
}

async function settleNavigation() {
  await nextTick();
  await Promise.resolve();
}

function subject(hash = "#resources", { enabled = true, busy = false, failSynchronously = false, refreshOverride }: SubjectOptions = {}) {
  const activeTab = ref("resources");
  const available = ref([{ key: "resources" }, { key: "tasks" }]);
  const page = ref(1), pageSize = ref(20), selectedResource = ref<Resource>();
  const authenticated = ref(enabled), sessionUser = ref(enabled ? { mustChangePassword: false } : undefined), loading = ref(busy);
  const writes: string[] = [], refreshes: string[] = [], errors: unknown[] = [];
  const navigation = useWorkspaceNavigation({
    activeTab, available, page, pageSize, selectedResource, authenticated, sessionUser, busy: loading,
    readHash: () => hash.slice(1), writeHash: (key) => writes.push(key),
    refresh: (quiet) => {
      if (failSynchronously) throw new Error("同步刷新失败");
      if (refreshOverride) return refreshOverride(quiet);
      refreshes.push(`${activeTab.value}:${page.value}:${selectedResource.value?.id ?? ""}`);
    },
    onError: (error) => errors.push(error),
  });
  return { activeTab, available, page, selectedResource, authenticated, sessionUser, loading, writes, refreshes, errors, navigation };
}

describe("useWorkspaceNavigation", () => {
  it("从资源任务切入任务页时清除资源筛选并只刷新一次", async () => {
    const state = subject();
    state.page.value = 2;
    state.navigation.viewResourceTasks(resource);
    await settleNavigation();

    expect(state.activeTab.value).toBe("tasks");
    expect(state.page.value).toBe(1);
    expect(state.selectedResource.value).toEqual(resource);
    expect(state.writes).toEqual(["tasks"]);
    expect(state.refreshes).toEqual(["tasks:1:camera"]);
    expect(state.errors).toEqual([]);
  });

  it("任务页重复导航会回到第一页并只读取最终筛选快照", async () => {
    const state = subject();
    state.activeTab.value = "tasks";
    state.page.value = 2;
    await settleNavigation();
    state.refreshes.splice(0);
    state.writes.splice(0);

    state.navigation.navigate("tasks");
    await settleNavigation();

    expect(state.page.value).toBe(1);
    expect(state.selectedResource.value).toBeUndefined();
    expect(state.refreshes).toEqual(["tasks:1:"]);
  });

  it("任务页已有资源筛选时点击同页导航会清筛选并刷新", async () => {
    const state = subject();
    state.activeTab.value = "tasks";
    state.selectedResource.value = resource;
    await settleNavigation();
    state.refreshes.splice(0);

    state.navigation.navigate("tasks");
    await settleNavigation();

    expect(state.selectedResource.value).toBeUndefined();
    expect(state.refreshes).toEqual(["tasks:1:"]);
  });

  it("哈希恢复仅接受当前账号可访问的页面", async () => {
    const state = subject("#tasks");
    state.navigation.restoreWorkspaceFromHash();
    await settleNavigation();
    expect(state.activeTab.value).toBe("tasks");
    expect(state.writes).toEqual(["tasks"]);

    state.available.value = [{ key: "resources" }];
    await settleNavigation();
    expect(state.activeTab.value).toBe("resources");
    expect(state.writes).toEqual(["tasks", "resources"]);
  });

  it("首次挂载会写入默认资源页哈希但不刷新", async () => {
    const state = subject();
    state.navigation.syncWorkspaceHash();
    await settleNavigation();
    expect(state.writes).toEqual(["resources"]);
    expect(state.refreshes).toEqual([]);
  });

  it("退出时不改写默认资源页，下一次会话启用权限后才回退", async () => {
    const state = subject("#tasks", { enabled: false });
    state.navigation.restoreWorkspaceFromHash();
    await settleNavigation();
    expect(state.activeTab.value).toBe("resources");

    state.available.value = [{ key: "api-reference" }];
    await settleNavigation();
    expect(state.activeTab.value).toBe("resources");
    state.sessionUser.value = { mustChangePassword: false };
    state.authenticated.value = true;
    await settleNavigation();
    expect(state.activeTab.value).toBe("api-reference");
  });

  it("忙碌时保留导航刷新，解除忙碌后只读取一次", async () => {
    const state = subject("#resources", { busy: true });
    state.navigation.navigate("tasks");
    await settleNavigation();
    expect(state.refreshes).toEqual([]);

    state.loading.value = false;
    await settleNavigation();
    expect(state.refreshes).toEqual(["tasks:1:"]);
  });

  it("登出期间不刷新，重新登录后执行保留的最终导航意图", async () => {
    const state = subject("#resources", { busy: true });
    state.navigation.navigate("tasks");
    state.authenticated.value = false;
    state.sessionUser.value = undefined;
    state.loading.value = false;
    await settleNavigation();
    expect(state.refreshes).toEqual([]);

    state.sessionUser.value = { mustChangePassword: false };
    state.authenticated.value = true;
    await settleNavigation();
    expect(state.refreshes).toEqual(["tasks:1:"]);
  });

  it("同步刷新异常会交给统一错误处理", async () => {
    const state = subject("#resources", { failSynchronously: true });
    state.navigation.navigate("tasks");
    await settleNavigation();
    expect(state.errors).toHaveLength(1);
    expect((state.errors[0] as Error).message).toBe("同步刷新失败");
  });

  it("慢速静默轮询不重叠，用户切页会在完成后优先执行普通刷新", async () => {
    const first = deferred<void>();
    const quietModes: boolean[] = [];
    const state = subject("#resources", { refreshOverride: (quiet) => {
      quietModes.push(quiet);
      return quietModes.length === 1 ? first.promise : undefined;
    } });
    state.navigation.requestRefresh(true);
    await settleNavigation();
    expect(quietModes).toEqual([true]);

    state.navigation.requestRefresh(true);
    state.navigation.requestRefresh(true);
    state.navigation.navigate("tasks");
    await settleNavigation();
    expect(quietModes).toEqual([true]);

    first.resolve();
    await settleNavigation();
    await settleNavigation();
    expect(quietModes).toEqual([true, false]);
  });

  it("被拒绝的手动刷新不会锁住后续刷新", async () => {
    const failure = new Error("轮询失败");
    let attempts = 0;
    const state = subject("#resources", { refreshOverride: () => {
      attempts += 1;
      return attempts === 1 ? Promise.reject(failure) : undefined;
    } });
    state.navigation.requestRefresh();
    await settleNavigation();
    await settleNavigation();
    state.navigation.requestRefresh();
    await settleNavigation();
    expect(attempts).toBe(2);
    expect(state.errors).toEqual([failure]);
  });

  it("卸载后取消已排队的刷新", async () => {
    const scope = effectScope();
    let state!: ReturnType<typeof subject>;
    scope.run(() => { state = subject(); });
    state.navigation.navigate("tasks");
    scope.stop();
    await settleNavigation();
    expect(state.refreshes).toEqual([]);
  });
});
