// 工作区列表协调只管理远端快照、筛选与失效代次；导航和编辑器装配仍由 App 负责。
import { computed, ref, watch, type Ref } from "vue";

import type { Node, Page, Resource, Task, Template } from "../shared/types";
import { anchorNodeSnapshot } from "../features/nodes/nodeDashboard";

interface WorkspaceUser {
  id: string;
  isAdmin: boolean;
}
interface WorkspaceApi {
  tasks: (page?: number, pageSize?: number, filters?: {
    search?: string; status?: string; resourceId?: string; createdBy?: string;
  }) => Promise<Page<Task>>;
  templates: (page?: number, pageSize?: number, filters?: Record<string, string | undefined>) => Promise<Page<Template>>;
  nodes: (page?: number, pageSize?: number) => Promise<Page<Node>>;
}
interface WorkspaceCollectionOptions {
  user: Ref<WorkspaceUser | undefined>;
  sessionGeneration: Ref<number>;
  can: (scope: string) => boolean;
  api: WorkspaceApi;
  onError: (value: unknown) => void;
}

/** 列表查询绑定筛选快照与会话代次，防止旧响应污染当前工作区。 */
export function useWorkspaceCollections(options: WorkspaceCollectionOptions) {
  const tasks = ref<Task[]>([]);
  const templates = ref<Template[]>([]);
  const nodes = ref<Node[]>([]);
  const totals = ref({ tasks: 0, templates: 0, nodes: 0 });
  const page = ref(1);
  const pageSize = ref(20);
  const templatePage = ref(1);
  const taskSearch = ref("");
  const taskStatus = ref("");
  const taskCreatedBy = ref("");
  const taskShowAll = ref(false);
  const templateCreatedBy = ref("");
  const templateShowAll = ref(false);
  const templateIncludeDeleted = ref(false);
  const selectedResource = ref<Resource>();
  const lastUpdated = ref("");
  let taskGeneration = 0;

  const taskSelectionKey = computed(() => [
    page.value, taskSearch.value, taskStatus.value, selectedResource.value?.id ?? "",
    taskShowAll.value, taskCreatedBy.value, options.user.value?.id ?? "",
  ].join("\u0000"));
  const templateSelectionKey = computed(() => [
    templatePage.value, templateShowAll.value, templateCreatedBy.value,
    templateIncludeDeleted.value, options.user.value?.id ?? "",
  ].join("\u0000"));

  async function loadTasks() {
    if (!options.can("tasks:read")) return;
    const current = ++taskGeneration;
    const requestedSelection = taskSelectionKey.value;
    const data = await options.api.tasks(page.value, pageSize.value, {
      search: taskSearch.value.trim() || undefined,
      status: taskStatus.value || undefined,
      resourceId: selectedResource.value?.id,
      createdBy: taskShowAll.value ? taskCreatedBy.value || undefined : options.user.value?.id,
    });
    // 导航已先更新筛选、但下一次请求尚未调度时，也不能短暂显示旧筛选的响应。
    if (current !== taskGeneration || requestedSelection !== taskSelectionKey.value) return;
    tasks.value = data.items;
    totals.value.tasks = data.total;
    lastUpdated.value = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  }
  async function loadTemplates() {
    if (!options.can("templates:read")) return;
    const currentSession = options.sessionGeneration.value;
    const requestedPage = templatePage.value;
    const requestedSelection = templateSelectionKey.value;
    const data = await options.api.templates(requestedPage, 100, {
      createdBy: templateShowAll.value ? templateCreatedBy.value || undefined : options.user.value?.id,
      includeDeleted: templateIncludeDeleted.value ? "true" : undefined,
    });
    if (requestedPage !== templatePage.value || requestedSelection !== templateSelectionKey.value ||
      currentSession !== options.sessionGeneration.value) return;
    templates.value = data.items;
    totals.value.templates = data.total;
  }
  async function loadNodes() {
    if (!options.user.value?.isAdmin) return;
    const currentSession = options.sessionGeneration.value;
    const data = await options.api.nodes();
    if (currentSession !== options.sessionGeneration.value) return;
    nodes.value = data.items.map(anchorNodeSnapshot);
    totals.value.nodes = data.total;
  }
  function clear() {
    ++taskGeneration;
    tasks.value = [];
    templates.value = [];
    nodes.value = [];
  }

  watch(templatePage, () => void loadTemplates().catch(options.onError));
  watch(() => [options.user.value?.id, options.user.value?.isAdmin], () => {
    templateShowAll.value = Boolean(options.user.value?.isAdmin);
    templateCreatedBy.value = "";
    templateIncludeDeleted.value = false;
    templatePage.value = 1;
  }, { immediate: true });

  return {
    tasks, templates, nodes, totals, page, pageSize, templatePage,
    taskSearch, taskStatus, taskCreatedBy, taskShowAll, taskSelectionKey,
    templateCreatedBy, templateShowAll, templateIncludeDeleted, templateSelectionKey,
    selectedResource, lastUpdated, loadTasks, loadTemplates, loadNodes, clear,
  };
}
