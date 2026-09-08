<script setup lang="ts">
// 根协调层只保存会话、页签和列表数据；具体编辑器与业务动作下沉到 feature 目录。
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { KeyRound, RefreshCw, Terminal, Plus, Radio, FileCode2, Server, LogOut, Search, ChevronRight, BookOpen, ShieldCheck, ScrollText, Settings2, PanelLeftClose, PanelLeftOpen, HardDrive } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import { api, clearToken, getToken, setToken } from "../shared/api";
import type { Node, Resource, Task, Template } from "../shared/types";
import { taskStatusLabels } from "../features/tasks/taskStatus";
import AsyncView from "../shared/AsyncView.vue";

const loadTaskOverview = () => import("../features/tasks/TaskOverview.vue");
const loadTaskList = () => import("../features/tasks/TaskList.vue");
const loadTaskEditor = () => import("../features/tasks/TaskEditor.vue");
const loadTemplateList = () => import("../features/templates/TemplateList.vue");
const loadTemplateEditor = () => import("../features/templates/TemplateEditor.vue");
const loadNodeList = () => import("../features/nodes/NodeList.vue");
const loadLogsWorkspace = () => import("../features/logs/LogsWorkspace.vue");
const loadAccessManager = () => import("../features/access/AccessManager.vue");
const loadAuditWorkspace = () => import("../features/audit/AuditWorkspace.vue");
const loadSettingsManager = () => import("../features/settings/SettingsManager.vue");
const loadResourceWorkspace = () => import("../features/resources/ResourceWorkspace.vue");
const navigation = [
  { key: "resources", label: "设备资源", icon: HardDrive },
  { key: "tasks", label: "采集任务", icon: Radio },
  { key: "logs", label: "日志工作台", icon: Terminal },
  { key: "templates", label: "命令模板", icon: FileCode2 },
  { key: "nodes", label: "服务节点", icon: Server },
  { key: "access", label: "服务账号", icon: ShieldCheck },
  { key: "audit", label: "审计与事件", icon: ScrollText },
  { key: "settings", label: "后台配置", icon: Settings2 },
];
const lastUpdated = ref("");
const sidebarCollapsed = ref(localStorage.getItem("camera-log-sidebar-collapsed") === "true");
function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value;
  try { localStorage.setItem("camera-log-sidebar-collapsed", String(sidebarCollapsed.value)); }
  catch { /* 浏览器禁用存储时本次布局切换仍然有效。 */ }
}
const templatePage = ref(1);
const selectedWorkspace = ref("config");
const selectedLogTask = ref("");
const activeTab = ref("resources"),
  token = ref(getToken()),
  authenticated = ref(Boolean(getToken())),
  busy = ref(false);
const tasks = ref<Task[]>([]),
  templates = ref<Template[]>([]),
  nodes = ref<Node[]>([]);
const totals = ref({ tasks: 0, templates: 0, nodes: 0 }),
  page = ref(1),
  pageSize = ref(20);
const taskSearch = ref(""),
  taskStatus = ref("");
const selectedTask = ref<Task>(),
  selectedResource = ref<Resource>(),
  selectedTemplate = ref<Template>(),
  taskEditorOpen = ref(false),
  templateEditorOpen = ref(false);
const resourceWorkspace = ref<InstanceType<typeof AsyncView>>();
let taskGeneration = 0;
const error = (value: unknown) =>
  ElMessage.error(value instanceof Error ? value.message : "请求失败");
async function loadTasks() {
  const current = ++taskGeneration;
  const data = await api.tasks(page.value, pageSize.value, {
    search: taskSearch.value.trim() || undefined,
    status: taskStatus.value || undefined,
    resourceId: selectedResource.value?.id,
  });
  if (current !== taskGeneration) return;
  tasks.value = data.items;
  totals.value.tasks = data.total;
  lastUpdated.value = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}
async function loadTemplates() {
  const requestedPage = templatePage.value;
  const data = await api.templates(requestedPage, 100);
  if (requestedPage !== templatePage.value) return;
  templates.value = data.items;
  totals.value.templates = data.total;
}
async function loadNodes() {
  const data = await api.nodes();
  nodes.value = data.items;
  totals.value.nodes = data.total;
}
// 静默刷新供轮询使用，避免网络暂态在用户未操作时反复弹出错误消息。
async function refresh(quiet = false) {
  if (!authenticated.value || busy.value) return;
  if (!quiet) busy.value = true;
  try {
    if (activeTab.value === "resources") await resourceWorkspace.value?.reload();
    else if (activeTab.value === "tasks") await loadTasks();
    else if (activeTab.value === "templates") await loadTemplates();
    else if (activeTab.value === "nodes") await loadNodes();
  } catch (value) {
    if (!quiet) error(value);
  } finally {
    if (!quiet) busy.value = false;
  }
}
async function login() {
  if (!token.value.trim()) return ElMessage.warning("请输入访问令牌");
  setToken(token.value);
  busy.value = true;
  try {
    await Promise.all([loadTasks(), loadTemplates(), loadNodes()]);
    authenticated.value = true;
  } catch (value) {
    authenticated.value = false;
    clearToken();
    error(value);
  } finally {
    busy.value = false;
  }
}
function logout() {
  ++taskGeneration;
  clearToken();
  token.value = "";
  authenticated.value = false;
  tasks.value = [];
  templates.value = [];
  nodes.value = [];
  taskEditorOpen.value = false;
  templateEditorOpen.value = false;
  selectedResource.value = undefined;
  selectedTask.value = undefined;
  selectedLogTask.value = "";
  activeTab.value = "resources";
}
function createTask() {
  selectedWorkspace.value = "config";
  selectedTask.value = undefined;
  taskEditorOpen.value = true;
  void loadTemplates().catch(error);
}
function viewResourceTasks(resource: Resource) {
  selectedResource.value = resource;
  page.value = 1;
  activeTab.value = "tasks";
}
function navigate(key: string) {
  if (key === "tasks") {
    selectedResource.value = undefined;
    page.value = 1;
    void loadTasks().catch(error);
  }
  activeTab.value = key;
}
function editTask(task: Task) {
  selectedWorkspace.value = "config";
  selectedTask.value = task;
  taskEditorOpen.value = true;
  void loadTemplates().catch(error);
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
  void refresh();
}
watch(activeTab, () => {
  page.value = 1;
  void refresh();
});
watch([page, pageSize], () => void refresh());
watch(templatePage, () => void loadTemplates().catch(error));
let timer: ReturnType<typeof setInterval>;
onMounted(() => {
  if (authenticated.value) void login();
  timer = setInterval(() => void refresh(true), 5000);
});
onBeforeUnmount(() => clearInterval(timer));
</script>
<template>
  <el-config-provider :locale="zhCn">
  <main class="shell" :class="{ 'is-authenticated': authenticated, 'sidebar-collapsed': sidebarCollapsed }">
    <aside v-if="authenticated" class="sidebar">
      <div class="sidebar-brand"><span class="brand-mark"><Terminal :size="23" /></span><div><strong>设备日志服务</strong><small>LOG RECORD</small></div></div>
      <div class="nav-caption">工作空间</div>
      <nav role="tablist" aria-label="工作空间导航" class="side-nav">
        <button v-for="item in navigation" :key="item.key" role="tab" :aria-label="item.label" :title="sidebarCollapsed ? item.label : undefined" :aria-selected="activeTab === item.key" :class="{ active: activeTab === item.key }" @click="navigate(item.key)">
          <component :is="item.icon" :size="18" /><span>{{ item.label }}</span><ChevronRight v-if="activeTab === item.key" :size="14" />
        </button>
      </nav>
      <div class="sidebar-footer"><a href="https://github.com/geekingdom/camera-log-record-server/blob/main/docs/api.md" target="_blank" rel="noopener"><BookOpen :size="16" /> API 文档</a><span><i /> 已连接控制台</span></div>
    </aside>
    <div class="main-column">
    <header class="topbar">
      <div v-if="authenticated" class="breadcrumb"><el-tooltip :content="sidebarCollapsed ? '展开导航栏' : '折叠导航栏'"><el-button text :icon="sidebarCollapsed ? PanelLeftOpen : PanelLeftClose" :aria-label="sidebarCollapsed ? '展开导航栏' : '折叠导航栏'" :aria-expanded="!sidebarCollapsed" @click="toggleSidebar" /></el-tooltip><span>工作空间</span><ChevronRight :size="14" /><strong>{{ navigation.find(item => item.key === activeTab)?.label }}</strong></div>
      <div v-else class="brand"><Terminal :size="22" /><span>设备日志服务</span></div>
      <div v-if="authenticated" class="topbar-actions"><span class="refresh-time" v-if="lastUpdated">任务更新 {{ lastUpdated }}</span><el-tooltip content="退出控制台"><el-button text :icon="LogOut" aria-label="退出" @click="logout" /></el-tooltip></div>
    </header>
    <section v-if="!authenticated" class="login-state">
      <div class="login-symbol"><Terminal :size="30" /></div>
      <h1>设备日志服务</h1>
      <form class="auth" @submit.prevent="login">
        <el-input
          v-model="token"
          type="password"
          placeholder="访问令牌"
          aria-label="访问令牌"
        /><el-button
          native-type="submit"
          type="primary"
          :icon="KeyRound"
          :loading="busy"
          >连接</el-button
        >
      </form>
    </section>
    <section v-else class="workspace">
      <div class="page-head">
        <h1>
          {{ navigation.find(item => item.key === activeTab)?.label }}
        </h1>
        <div>
          <el-tooltip v-if="['resources', 'tasks', 'templates', 'nodes'].includes(activeTab)" content="刷新列表"
            ><el-button
              :icon="RefreshCw"
              aria-label="刷新列表"
              @click="refresh()" /></el-tooltip
          ><el-button
            v-if="activeTab === 'templates' || (activeTab === 'tasks' && !selectedResource?.deletedAt)"
            type="primary"
            :icon="Plus"
            @click="activeTab === 'tasks' ? createTask() : createTemplate()"
            >新建{{ activeTab === "tasks" ? "任务" : "模板" }}</el-button
          >
        </div>
      </div>
      <template v-if="activeTab === 'tasks'">
        <AsyncView :loader="loadTaskOverview" :component-props="{ tasks, total: totals.tasks, nodes: totals.nodes }" />
        <div class="list-heading"><h2>{{ selectedResource ? selectedResource.name + ' · 采集任务' : '全部采集任务' }}</h2><span>{{ totals.tasks }} 条记录</span><el-button v-if="selectedResource" text @click="selectedResource = undefined; applyTaskFilters()">查看全部任务</el-button></div>
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
            ><el-option v-for="(label, value) in taskStatusLabels" :key="value" :label="label" :value="value" /></el-select
          ><el-button @click="applyTaskFilters">筛选</el-button>
        </div>
        <AsyncView
          :loader="loadTaskList"
          :component-props="{ items: tasks, loading: busy }"
          :listeners="{ edit: editTask, view: viewTask, changed: () => refresh() }"
        />
      </template>
      <AsyncView v-else-if="activeTab === 'resources'" ref="resourceWorkspace" :loader="loadResourceWorkspace" :listeners="{ tasks: viewResourceTasks }" />
      <AsyncView
        v-else-if="activeTab === 'templates'"
        :loader="loadTemplateList"
        :component-props="{ items: templates }"
        :listeners="{ edit: editTemplate, changed: () => refresh() }"
      />
      <AsyncView v-else-if="activeTab === 'nodes'" :loader="loadNodeList" :component-props="{ items: nodes, loading: busy }" />
      <AsyncView v-else-if="activeTab === 'logs'" :loader="loadLogsWorkspace" :component-props="{ modelValue: selectedLogTask }" :listeners="{ 'update:modelValue': (value: string) => selectedLogTask = value }" />
      <AsyncView v-else-if="activeTab === 'access'" :loader="loadAccessManager" />
      <AsyncView v-else-if="activeTab === 'audit'" :loader="loadAuditWorkspace" />
      <AsyncView v-else-if="activeTab === 'settings'" :loader="loadSettingsManager" />
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
    <footer v-if="authenticated" class="workspace-footer"><span>设备日志记录平台</span><span>SSH / Telnet</span></footer>
    </div>
  </main>
  <AsyncView
    v-if="taskEditorOpen"
    overlay
    :loader="loadTaskEditor"
    :component-props="{ modelValue: taskEditorOpen, task: selectedTask, initialWorkspace: selectedWorkspace, initialResource: selectedResource, templates }"
    :listeners="{ 'update:modelValue': (value: boolean) => taskEditorOpen = value, saved: () => refresh() }"
  />
  <AsyncView
    v-if="templateEditorOpen"
    overlay
    :loader="loadTemplateEditor"
    :component-props="{ modelValue: templateEditorOpen, template: selectedTemplate }"
    :listeners="{ 'update:modelValue': (value: boolean) => templateEditorOpen = value, saved: () => refresh() }"
  />
  </el-config-provider>
</template>
