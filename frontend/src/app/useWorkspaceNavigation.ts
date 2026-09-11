/**
 * 工作区导航状态机。
 *
 * 将路由哈希、任务筛选重置和列表刷新收敛到同一响应式边界，避免点击导航时由
 * 事件处理器和页签监听器分别发起相同请求。刷新通过微任务合并：一次状态切换
 * 同时修改资源筛选、页码和页签时，列表只读取一次最终快照。
 */
import { computed, getCurrentScope, onScopeDispose, watch, type Ref } from "vue";

import type { Resource } from "../shared/types";

interface NavigationItem {
  key: string;
  label?: string;
}

interface SessionUser {
  mustChangePassword?: boolean;
}

export const DEFAULT_WORKSPACE = "resources";

interface WorkspaceNavigationOptions {
  activeTab: Ref<string>;
  available: Ref<NavigationItem[]>;
  page: Ref<number>;
  pageSize: Ref<number>;
  selectedResource: Ref<Resource | undefined>;
  authenticated: Ref<boolean>;
  sessionUser: Ref<SessionUser | undefined>;
  busy: Ref<boolean>;
  readHash: () => string;
  writeHash: (key: string) => void;
  refresh: (quiet: boolean) => Promise<void> | void;
  onError: (value: unknown) => void;
}

/** 管理工作区切换、筛选归位与哈希恢复，所有刷新都由此处合并调度。 */
export function useWorkspaceNavigation(options: WorkspaceNavigationOptions) {
  let refreshQueued = false;
  let refreshRequested = false;
  let requestedQuiet = true;
  let refreshInFlight = false;
  let disposed = false;
  const activeNavigationLabel = computed(() =>
    options.available.value.find((item) => item.key === options.activeTab.value)?.label,
  );

  function requestRefresh(quiet = false) {
    requestedQuiet = refreshRequested ? requestedQuiet && quiet : quiet;
    refreshRequested = true;
    scheduleRefresh();
  }

  function scheduleRefresh() {
    if (disposed || !refreshRequested || !canRefresh() || options.busy.value || refreshQueued || refreshInFlight) return;
    refreshQueued = true;
    queueMicrotask(() => {
      refreshQueued = false;
      if (disposed || !refreshRequested || !canRefresh() || options.busy.value || refreshInFlight) return;
      const quiet = requestedQuiet;
      refreshRequested = false;
      requestedQuiet = true;
      refreshInFlight = true;
      Promise.resolve().then(() => options.refresh(quiet)).catch((error) => {
        if (!quiet) options.onError(error);
      }).finally(() => {
        refreshInFlight = false;
        scheduleRefresh();
      });
    });
  }

  function selectFallback(items: NavigationItem[]) {
    return items.some((item) => item.key === DEFAULT_WORKSPACE)
      ? DEFAULT_WORKSPACE
      : items[0]?.key ?? "empty";
  }

  function canRefresh() {
    return options.authenticated.value && !options.sessionUser.value?.mustChangePassword;
  }

  function restoreWorkspaceFromHash() {
    if (!options.sessionUser.value) return;
    const key = options.readHash() || DEFAULT_WORKSPACE;
    options.activeTab.value = options.available.value.some((item) => item.key === key)
      ? key
      : selectFallback(options.available.value);
  }

  /** 首次挂载时补齐默认 Hash，但不触发列表读取。 */
  function syncWorkspaceHash() {
    options.writeHash(options.activeTab.value);
  }

  function navigate(key: string) {
    if (key === "tasks") {
      options.selectedResource.value = undefined;
      options.page.value = 1;
    }
    options.activeTab.value = key;
    // 同页重复点击时没有响应式字段变化，仍必须按重置后的筛选重新读取一次。
    requestRefresh();
  }

  function viewResourceTasks(resource: Resource) {
    options.selectedResource.value = resource;
    options.page.value = 1;
    options.activeTab.value = "tasks";
    requestRefresh();
  }

  watch([options.available, options.sessionUser], ([items, sessionUser]) => {
    if (sessionUser && !items.some((item) => item.key === options.activeTab.value))
      options.activeTab.value = selectFallback(items);
  });
  watch(options.activeTab, (key) => {
    options.writeHash(key);
    options.page.value = 1;
    requestRefresh();
  });
  watch([options.page, options.pageSize], () => requestRefresh());
  // 忙碌期间的导航意图不能直接丢弃；会话或加载完成后只读取最后一次状态快照。
  watch([options.authenticated, options.sessionUser, options.busy], scheduleRefresh);
  if (getCurrentScope()) onScopeDispose(() => { disposed = true; });

  return { activeNavigationLabel, navigate, requestRefresh, restoreWorkspaceFromHash, syncWorkspaceHash, viewResourceTasks };
}
