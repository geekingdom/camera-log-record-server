<script setup lang="ts">
// 根协调层只保存会话、页签和列表数据；具体编辑器与业务动作下沉到 feature 目录。
import { computed, onBeforeUnmount, onMounted, provide, ref } from "vue";
import {
  RefreshCw,
  Plus,
  Search,
} from "lucide-vue-next";
import { ElMessage } from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import { api } from "../shared/api";
import type { Resource, Task, Template } from "../shared/types";
import { taskStatusLabels } from "../features/tasks/taskStatus";
import AsyncView from "../shared/AsyncView.vue";
import { permissionKey } from "../shared/permissions";
import { canManageOwnedRecord } from "../shared/ownership";
import LoginPanel from "../features/auth/LoginPanel.vue";
import PasswordChangeDialog from "../features/auth/PasswordChangeDialog.vue";
import { usePlatformSession } from "../features/auth/usePlatformSession";
import AppNavigation from "./AppNavigation.vue";
import { useWorkspaceCollections } from "./useWorkspaceCollections";
import { DEFAULT_WORKSPACE, useWorkspaceNavigation } from "./useWorkspaceNavigation";
import { workspaceNavigation } from "./workspaceNavigation";

const loadTaskOverview = () => import("../features/tasks/TaskOverview.vue");
const loadTaskList = () => import("../features/tasks/TaskList.vue");
const loadTaskEditor = () => import("../features/tasks/TaskEditor.vue");
const loadTemplateList = () => import("../features/templates/TemplateList.vue");
const loadTemplateEditor = () => import("../features/templates/TemplateEditor.vue");
const loadNodeList = () => import("../features/nodes/NodeList.vue");
const loadLogsWorkspace = () => import("../features/logs/LogsWorkspace.vue");
const loadAccessManager = () => import("../features/access/AccountWorkspace.vue");
const loadAuditWorkspace = () => import("../features/audit/AuditWorkspace.vue");
const loadSettingsManager = () => import("../features/settings/SettingsManager.vue");
const loadResourceWorkspace = () => import("../features/resources/ResourceWorkspace.vue");
const loadApiReferenceWorkspace = () => import("../features/api/ApiReferenceWorkspace.vue");
const sidebarCollapsed = ref(
  localStorage.getItem("camera-log-sidebar-collapsed") === "true",
);
function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value;
  try {
    localStorage.setItem(
      "camera-log-sidebar-collapsed",
      String(sidebarCollapsed.value),
    );
  } catch {
    /* 浏览器禁用存储时本次布局切换仍然有效。 */
  }
}
const selectedWorkspace = ref("config");
const selectedLogTask = ref("");
const activeTab = ref(window.location.hash.slice(1) || DEFAULT_WORKSPACE),
  taskEditorOpen = ref(false),
  templateEditorOpen = ref(false);
const {
  authenticated,
  busy,
  user,
  passwordOpen,
  passwordSaving,
  generation: sessionGeneration,
  login,
  logout,
  changePassword,
  start: startSession,
  stop: stopSession,
} = usePlatformSession({
  loadInitial: async (sessionUser) => {
    taskShowAll.value = sessionUser.isAdmin;
    taskCreatedBy.value = "";
    await Promise.all([
      hasScope("tasks:read") && loadTasks(),
      hasScope("templates:read") && loadTemplates(),
      sessionUser.isAdmin && loadNodes(),
    ]);
  },
  clearWorkspace: () => {
    collections.clear();
    taskEditorOpen.value = false;
    templateEditorOpen.value = false;
    selectedTemplate.value = undefined;
    selectedResource.value = undefined;
    selectedTask.value = undefined;
    selectedLogTask.value = "";
    activeTab.value = "resources";
  },
  refreshWorkspace: async () => {
    navigation.requestRefresh();
  },
});
const hasScope = (scope?: string) =>
  !scope ||
  Boolean(
    user.value?.scopes.includes("*") || user.value?.scopes.includes(scope),
  );
const error = (value: unknown) =>
  ElMessage.error(value instanceof Error ? value.message : "请求失败");
const collections = useWorkspaceCollections({
  user,
  sessionGeneration,
  can: hasScope,
  api,
  onError: error,
});
const {
  tasks,
  templates,
  nodes,
  totals,
  page,
  pageSize,
  templatePage,
  taskSearch,
  taskStatus,
  taskCreatedBy,
  taskShowAll,
  taskSelectionKey,
  templateCreatedBy,
  templateShowAll,
  templateIncludeDeleted,
  templateSelectionKey,
  selectedResource,
  lastUpdated,
  loadTasks,
  loadTemplates,
  loadNodes,
} = collections;
provide(permissionKey, {
  can: (scope: string) => Boolean(user.value && hasScope(scope)),
  resource: () => Boolean(user.value),
  allResources: () => Boolean(user.value),
});
const visibleNavigation = computed(() =>
  workspaceNavigation.filter(
    (item) => (!item.admin || user.value?.isAdmin) && hasScope(item.scope),
  ).map(item => item.key === "access" && !user.value?.isAdmin ? { ...item, label: "我的服务账号" } : item),
);
const selectedTask = ref<Task>(),
  selectedTemplate = ref<Template>();
const resourceWorkspace = ref<InstanceType<typeof AsyncView>>();
// 静默刷新供轮询使用，避免网络暂态在用户未操作时反复弹出错误消息。
async function refresh(quiet = false) {
  const refreshGeneration = sessionGeneration.value;
  if (!authenticated.value || busy.value || user.value?.mustChangePassword)
    return;
  if (!quiet) busy.value = true;
  try {
    if (activeTab.value === "resources")
      await resourceWorkspace.value?.reload();
    else if (activeTab.value === "tasks") await loadTasks();
    else if (activeTab.value === "templates") await loadTemplates();
    else if (activeTab.value === "nodes") await loadNodes();
  } catch (value) {
    if (!quiet) error(value);
  } finally {
    if (!quiet && refreshGeneration === sessionGeneration.value) busy.value = false;
  }
}
function createTask(resource?: Resource) {
  if (!hasScope("tasks:create")) return;
  if (resource) {
    if (resource.deletedAt) return;
    selectedResource.value = resource;
  }
  selectedWorkspace.value = "config";
  selectedTask.value = undefined;
  taskEditorOpen.value = true;
  if (hasScope("templates:read")) void loadTemplates().catch(error);
}
const navigation = useWorkspaceNavigation({
  activeTab,
  available: visibleNavigation,
  page,
  pageSize,
  selectedResource,
  authenticated,
  sessionUser: user,
  busy,
  readHash: () => window.location.hash.slice(1),
  writeHash: (key) => {
    const next = `#${key}`;
    if (window.location.hash !== next) window.history.replaceState(null, "", next);
  },
  refresh: (quiet) => refresh(quiet),
  onError: error,
});
const { activeNavigationLabel, navigate, restoreWorkspaceFromHash, syncWorkspaceHash, viewResourceTasks } = navigation;
function editTask(task: Task) {
  if (!hasScope("tasks:write") || !owns(task)) return;
  selectedWorkspace.value = "config";
  selectedTask.value = task;
  taskEditorOpen.value = true;
  if (hasScope("templates:read")) void loadTemplates().catch(error);
}
function owns(item: { createdBy?: string }) {
  return canManageOwnedRecord(user.value?.id, user.value?.isAdmin, item.createdBy);
}
function viewTask(task: Task) {
  selectedLogTask.value = task.id;
  activeTab.value = "logs";
}
function createTemplate() {
  selectedTemplate.value = undefined;
  templateEditorOpen.value = true;
}
function editTemplate(template: Template) {
  selectedTemplate.value = template;
  templateEditorOpen.value = true;
}
function applyTaskFilters() {
  page.value = 1;
  navigation.requestRefresh();
}
function applyTaskOwnershipFilters(filters: { createdBy: string; showAll: boolean }) {
  taskCreatedBy.value = filters.createdBy;
  taskShowAll.value = filters.showAll;
  page.value = 1;
  navigation.requestRefresh();
}
function applyTemplateFilters(filters: {
  createdBy: string;
  showAll: boolean;
  includeDeleted: boolean;
}) {
  templateCreatedBy.value = filters.createdBy;
  templateShowAll.value = filters.showAll;
  templateIncludeDeleted.value = filters.includeDeleted;
  templatePage.value = 1;
  navigation.requestRefresh();
}
let timer: ReturnType<typeof setInterval>;
onMounted(() => {
  syncWorkspaceHash();
  startSession();
  window.addEventListener("hashchange", restoreWorkspaceFromHash);
  timer = setInterval(() => navigation.requestRefresh(true), 5000);
});
onBeforeUnmount(() => {
  clearInterval(timer);
  window.removeEventListener("hashchange", restoreWorkspaceFromHash);
  stopSession();
});
</script>
<template>
  <el-config-provider :locale="zhCn">
    <main
      class="shell"
      :class="{
        'is-authenticated': authenticated,
        'sidebar-collapsed': sidebarCollapsed,
      }"
    >
      <AppNavigation
        :authenticated="authenticated"
        :items="visibleNavigation"
        :active-tab="activeTab"
        :sidebar-collapsed="sidebarCollapsed"
        :display-name="user?.displayName"
        :last-updated="lastUpdated"
        @navigate="navigate"
        @toggle-sidebar="toggleSidebar"
        @open-password="passwordOpen = true"
        @logout="logout"
      >
        <LoginPanel v-if="!authenticated" :busy="busy" :login="login" />
        <section v-else-if="!user?.mustChangePassword" class="workspace">
          <el-empty
            v-if="activeTab === 'empty'"
            description="当前账号没有可用的平台权限"
          />
          <div class="page-head">
            <h1>
              {{ activeNavigationLabel }}
            </h1>
            <div>
              <el-tooltip
                v-if="
                  ['resources', 'tasks', 'templates', 'nodes'].includes(
                    activeTab,
                  )
                "
                content="刷新列表"
                ><el-button
                  :icon="RefreshCw"
                  aria-label="刷新列表"
                  @click="navigation.requestRefresh()" /></el-tooltip
              ><el-button
                v-if="
                  (activeTab === 'templates' && hasScope('templates:write')) ||
                  (activeTab === 'tasks' &&
                    hasScope('tasks:create') &&
                    !selectedResource?.deletedAt)
                "
                type="primary"
                :icon="Plus"
                @click="activeTab === 'tasks' ? createTask() : createTemplate()"
                >新建{{ activeTab === "tasks" ? "任务" : "模板" }}</el-button
              >
            </div>
          </div>
          <template v-if="activeTab === 'tasks'">
            <AsyncView
              :loader="loadTaskOverview"
              :component-props="{
                tasks,
                total: totals.tasks,
                nodes: totals.nodes,
              }"
            />
            <div class="list-heading">
              <h2>
                {{
                  selectedResource
                    ? selectedResource.name + " · 采集任务"
                    : "全部采集任务"
                }}
              </h2>
              <span>{{ totals.tasks }} 条记录</span
              ><el-button
                v-if="selectedResource"
                text
                @click="
                  selectedResource = undefined;
                  applyTaskFilters();
                "
                >查看全部任务</el-button
              >
            </div>
            <div class="task-filters">
              <el-input
                v-model="taskSearch"
                clearable
                placeholder="搜索任务名称或 IP"
                aria-label="搜索任务名称或 IP"
                :prefix-icon="Search"
                @keyup.enter="applyTaskFilters"
              /><el-select
                v-model="taskStatus"
                clearable
                placeholder="全部状态"
                aria-label="按状态筛选"
                @change="applyTaskFilters"
                ><el-option
                  v-for="(label, value) in taskStatusLabels"
                  :key="value"
                  :label="label"
                  :value="value" /></el-select
              ><el-button @click="applyTaskFilters">筛选</el-button>
            </div>
            <AsyncView
              :loader="loadTaskList"
              :component-props="{
                items: tasks,
                loading: busy,
                canWrite: hasScope('tasks:write'),
                canControl: hasScope('tasks:control'),
                userId: user?.id,
                isAdmin: user?.isAdmin,
                createdBy: taskCreatedBy,
                showAll: taskShowAll,
                selectionKey: taskSelectionKey,
              }"
              :listeners="{
                edit: editTask,
                view: viewTask,
                changed: () => navigation.requestRefresh(),
                filters: applyTaskOwnershipFilters,
              }"
            />
          </template>
          <AsyncView
            v-else-if="activeTab === 'resources'"
            ref="resourceWorkspace"
            :loader="loadResourceWorkspace"
            :component-props="{
              canWrite: hasScope('resources:write'),
              canCreate: hasScope('resources:create'),
              canCreateTask: hasScope('tasks:create'),
              canControl: hasScope('tasks:control'),
              userId: user?.id,
              isAdmin: user?.isAdmin,
            }"
            :listeners="{ tasks: viewResourceTasks, createTask }"
          />
          <AsyncView
            v-else-if="activeTab === 'templates'"
            :loader="loadTemplateList"
            :component-props="{
              items: templates,
              canWrite: hasScope('templates:write'),
              userId: user?.id,
              isAdmin: user?.isAdmin,
              createdBy: templateCreatedBy,
              showAll: templateShowAll,
              includeDeleted: templateIncludeDeleted,
              selectionKey: templateSelectionKey,
            }"
            :listeners="{ edit: editTemplate, changed: () => navigation.requestRefresh(), filters: applyTemplateFilters }"
          />
          <AsyncView
            v-else-if="activeTab === 'nodes'"
            :loader="loadNodeList"
            :component-props="{ items: nodes, loading: busy }"
          />
          <AsyncView
            v-else-if="activeTab === 'logs'"
            :loader="loadLogsWorkspace"
            :component-props="{
              modelValue: selectedLogTask,
              canDownload: hasScope('logs:download'),
              canSend: hasScope('commands:send'),
              userId: user?.id,
              isAdmin: user?.isAdmin,
            }"
            :listeners="{
              'update:modelValue': (value: string) => (selectedLogTask = value),
            }"
          />
          <AsyncView
            v-else-if="activeTab === 'api-reference'"
            :loader="loadApiReferenceWorkspace"
          />
          <AsyncView
            v-else-if="activeTab === 'access'"
            :loader="loadAccessManager"
            :component-props="{ isAdmin: Boolean(user?.isAdmin) }"
          />
          <AsyncView
            v-else-if="activeTab === 'audit'"
            :loader="loadAuditWorkspace"
          />
          <AsyncView
            v-else-if="activeTab === 'settings'"
            :loader="loadSettingsManager"
          />
          <el-pagination
            v-if="activeTab === 'templates'"
            v-model:current-page="templatePage"
            :page-size="100"
            :total="totals.templates"
            layout="total, prev, pager, next"
          />
          <el-pagination
            v-if="activeTab === 'tasks'"
            v-model:current-page="page"
            v-model:page-size="pageSize"
            :total="totals.tasks"
            :page-sizes="[20, 50, 100]"
            layout="total, prev, pager, next"
          />
        </section>
        <footer v-if="authenticated" class="workspace-footer">
          <span>设备日志记录平台</span><span>SSH / Telnet</span>
        </footer>
      </AppNavigation>
    </main>
    <AsyncView
      v-if="taskEditorOpen"
      overlay
      :loader="loadTaskEditor"
      :component-props="{
        modelValue: taskEditorOpen,
        task: selectedTask,
        initialWorkspace: selectedWorkspace,
        initialResource: selectedResource,
        templates,
        canEdit: !selectedTask || owns(selectedTask),
      }"
      :listeners="{
        'update:modelValue': (value: boolean) => (taskEditorOpen = value),
        saved: () => navigation.requestRefresh(),
      }"
    />
    <AsyncView
      v-if="templateEditorOpen"
      overlay
      :loader="loadTemplateEditor"
      :component-props="{
        modelValue: templateEditorOpen,
        template: selectedTemplate,
        userId: user?.id,
        isAdmin: user?.isAdmin,
      }"
      :listeners="{
        'update:modelValue': (value: boolean) => (templateEditorOpen = value),
        saved: () => navigation.requestRefresh(),
      }"
    />
  </el-config-provider>
  <PasswordChangeDialog
    :open="passwordOpen"
    :required="Boolean(user?.mustChangePassword)"
    :saving="passwordSaving"
    :change-password="changePassword"
    @update:open="passwordOpen = $event"
    @logout="logout"
  />
</template>
