<script setup lang="ts">
// 根协调层只保存会话、页签和列表数据；具体编辑器与业务动作下沉到 feature 目录。
import { computed, onBeforeUnmount, onMounted, provide, ref, watch } from "vue";
import {
  KeyRound,
  RefreshCw,
  Terminal,
  Plus,
  Radio,
  FileCode2,
  Server,
  LogOut,
  Search,
  ChevronRight,
  BookOpen,
  ShieldCheck,
  ScrollText,
  Settings2,
  PanelLeftClose,
  PanelLeftOpen,
  HardDrive,
} from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { confirmAction } from "../shared/confirm";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import { api, authApi, clearToken, type SessionUser } from "../shared/api";
import type { Node, Resource, Task, Template } from "../shared/types";
import { taskStatusLabels } from "../features/tasks/taskStatus";
import AsyncView from "../shared/AsyncView.vue";
import { permissionKey } from "../shared/permissions";

const loadTaskOverview = () => import("../features/tasks/TaskOverview.vue");
const loadTaskList = () => import("../features/tasks/TaskList.vue");
const loadTaskEditor = () => import("../features/tasks/TaskEditor.vue");
const loadTemplateList = () => import("../features/templates/TemplateList.vue");
const loadTemplateEditor = () =>
  import("../features/templates/TemplateEditor.vue");
const loadNodeList = () => import("../features/nodes/NodeList.vue");
const loadLogsWorkspace = () => import("../features/logs/LogsWorkspace.vue");
const loadAccessManager = () =>
  import("../features/access/AccountWorkspace.vue");
const loadAuditWorkspace = () => import("../features/audit/AuditWorkspace.vue");
const loadSettingsManager = () =>
  import("../features/settings/SettingsManager.vue");
const loadResourceWorkspace = () =>
  import("../features/resources/ResourceWorkspace.vue");
const navigation = [
  { key: "resources", label: "设备资源", icon: HardDrive, scope: "tasks:read" },
  { key: "tasks", label: "采集任务", icon: Radio, scope: "tasks:read" },
  { key: "logs", label: "日志工作台", icon: Terminal, scope: "logs:read" },
  {
    key: "templates",
    label: "命令模板",
    icon: FileCode2,
    scope: "templates:read",
  },
  { key: "nodes", label: "服务节点", icon: Server, admin: true },
  { key: "access", label: "账号管理", icon: ShieldCheck, admin: true },
  { key: "audit", label: "审计与事件", icon: ScrollText, admin: true },
  { key: "settings", label: "后台配置", icon: Settings2, admin: true },
];
const lastUpdated = ref("");
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
const templatePage = ref(1);
const selectedWorkspace = ref("config");
const selectedLogTask = ref("");
const activeTab = ref("resources"),
  authenticated = ref(false),
  busy = ref(false);
const user = ref<SessionUser>();
const loginForm = ref({ username: "", password: "" });
const passwordForm = ref({ current: "", next: "" });
const passwordOpen = ref(false);
const passwordSaving = ref(false);
const hasScope = (scope?: string) =>
  !scope ||
  Boolean(
    user.value?.scopes.includes("*") || user.value?.scopes.includes(scope),
  );
provide(permissionKey, {
  can: (scope: string) => Boolean(user.value && hasScope(scope)),
  resource: (id?: string) =>
    Boolean(
      user.value &&
        (!user.value.resourceIds ||
          (id && user.value.resourceIds.includes(id))),
    ),
  allResources: () => user.value?.resourceIds === null,
});
const visibleNavigation = computed(() =>
  navigation.filter(
    (item) => (!item.admin || user.value?.isAdmin) && hasScope(item.scope),
  ),
);
watch(visibleNavigation, (items) => {
  if (!items.some((item) => item.key === activeTab.value))
    activeTab.value = items[0]?.key ?? "empty";
});
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
let sessionGeneration = 0;
const error = (value: unknown) =>
  ElMessage.error(value instanceof Error ? value.message : "请求失败");
async function loadTasks() {
  if (!hasScope("tasks:read")) return;
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
  if (!hasScope("templates:read")) return;
  const currentSession = sessionGeneration;
  const requestedPage = templatePage.value;
  const data = await api.templates(requestedPage, 100);
  if (
    requestedPage !== templatePage.value ||
    currentSession !== sessionGeneration
  )
    return;
  templates.value = data.items;
  totals.value.templates = data.total;
}
async function loadNodes() {
  if (!user.value?.isAdmin) return;
  const currentSession = sessionGeneration;
  const data = await api.nodes();
  if (currentSession !== sessionGeneration) return;
  nodes.value = data.items;
  totals.value.nodes = data.total;
}
// 静默刷新供轮询使用，避免网络暂态在用户未操作时反复弹出错误消息。
async function refresh(quiet = false) {
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
    if (!quiet) busy.value = false;
  }
}
async function login() {
  if (!loginForm.value.username.trim() || !loginForm.value.password)
    return ElMessage.warning("请输入用户名和密码");
  busy.value = true;
  const current = ++sessionGeneration;
  try {
    const loggedIn = await authApi.login(
      loginForm.value.username.trim(),
      loginForm.value.password,
    );
    if (current !== sessionGeneration) return;
    user.value = loggedIn.user;
    loginForm.value.password = "";
    passwordOpen.value = user.value.mustChangePassword;
    if (!user.value.mustChangePassword)
      await Promise.all([
        hasScope("tasks:read") && loadTasks(),
        hasScope("templates:read") && loadTemplates(),
        user.value.isAdmin && loadNodes(),
      ]);
    if (current === sessionGeneration) authenticated.value = true;
  } catch (value) {
    authenticated.value = false;
    error(value);
  } finally {
    busy.value = false;
  }
}
async function logout() {
  ++sessionGeneration;
  ++taskGeneration;
  authenticated.value = false;
  user.value = undefined;
  passwordOpen.value = false;
  passwordForm.value = { current: "", next: "" };
  taskEditorOpen.value = false;
  templateEditorOpen.value = false;
  selectedTemplate.value = undefined;
  clearToken();
  try {
    await authApi.logout();
  } catch {
    /* 会话已失效时仍完成本地退出。 */
  }
  clearToken();
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
async function changePassword() {
  if (passwordSaving.value) return;
  if (
    passwordForm.value.next.length < 12 ||
    passwordForm.value.next.length > 128
  )
    return ElMessage.warning("新密码长度须为 12 至 128 位");
  if (
    !(await confirmAction(
      "确认修改当前账号密码并使其他登录会话失效？",
      "确认修改密码",
    ))
  )
    return;
  passwordSaving.value = true;
  try {
    user.value = (
      await authApi.password(
        passwordForm.value.current,
        passwordForm.value.next,
      )
    ).user;
    passwordForm.value = { current: "", next: "" };
    passwordOpen.value = false;
    await refresh();
  } catch (value) {
    error(value);
  } finally {
    passwordSaving.value = false;
  }
}
function createTask() {
  if (!hasScope("tasks:create")) return;
  selectedWorkspace.value = "config";
  selectedTask.value = undefined;
  taskEditorOpen.value = true;
  if (hasScope("templates:read")) void loadTemplates().catch(error);
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
  if (!hasScope("tasks:write")) return;
  selectedWorkspace.value = "config";
  selectedTask.value = task;
  taskEditorOpen.value = true;
  if (hasScope("templates:read")) void loadTemplates().catch(error);
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
  const current = ++sessionGeneration;
  const restore = async () => {
    try {
      const restored = await authApi.me();
      if (current !== sessionGeneration) return;
      user.value = restored.user;
      authenticated.value = true;
      passwordOpen.value = user.value.mustChangePassword;
      if (!user.value.mustChangePassword)
        await Promise.all([
          hasScope("tasks:read") && loadTasks(),
          hasScope("templates:read") && loadTemplates(),
          user.value.isAdmin && loadNodes(),
        ]);
    } catch {
      clearToken();
    }
  };
  void restore();
  window.addEventListener("auth-required", logout);
  timer = setInterval(() => void refresh(true), 5000);
});
onBeforeUnmount(() => {
  clearInterval(timer);
  window.removeEventListener("auth-required", logout);
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
      <aside v-if="authenticated" class="sidebar">
        <div class="sidebar-brand">
          <span class="brand-mark"><Terminal :size="23" /></span>
          <div><strong>设备日志服务</strong><small>LOG RECORD</small></div>
        </div>
        <div class="nav-caption">工作空间</div>
        <nav role="tablist" aria-label="工作空间导航" class="side-nav">
          <button
            v-for="item in visibleNavigation"
            :key="item.key"
            role="tab"
            :aria-label="item.label"
            :title="sidebarCollapsed ? item.label : undefined"
            :aria-selected="activeTab === item.key"
            :class="{ active: activeTab === item.key }"
            @click="navigate(item.key)"
          >
            <component :is="item.icon" :size="18" /><span>{{ item.label }}</span
            ><ChevronRight v-if="activeTab === item.key" :size="14" />
          </button>
        </nav>
        <div class="sidebar-footer">
          <a
            href="https://github.com/geekingdom/camera-log-record-server/blob/main/docs/api.md"
            target="_blank"
            rel="noopener"
            ><BookOpen :size="16" /> API 文档</a
          ><span><i /> 已连接控制台</span>
        </div>
      </aside>
      <div class="main-column">
        <header class="topbar">
          <div v-if="authenticated" class="breadcrumb">
            <el-tooltip
              :content="sidebarCollapsed ? '展开导航栏' : '折叠导航栏'"
              ><el-button
                text
                :icon="sidebarCollapsed ? PanelLeftOpen : PanelLeftClose"
                :aria-label="sidebarCollapsed ? '展开导航栏' : '折叠导航栏'"
                :aria-expanded="!sidebarCollapsed"
                @click="toggleSidebar" /></el-tooltip
            ><span>工作空间</span><ChevronRight :size="14" /><strong>{{
              navigation.find((item) => item.key === activeTab)?.label
            }}</strong>
          </div>
          <div v-else class="brand">
            <Terminal :size="22" /><span>设备日志服务</span>
          </div>
          <div v-if="authenticated" class="topbar-actions">
            <span class="refresh-time">{{ user?.displayName }}</span
            ><span class="refresh-time" v-if="lastUpdated"
              >任务更新 {{ lastUpdated }}</span
            ><el-tooltip content="修改密码"
              ><el-button
                text
                :icon="KeyRound"
                aria-label="修改密码"
                @click="passwordOpen = true" /></el-tooltip
            ><el-tooltip content="退出控制台"
              ><el-button text :icon="LogOut" aria-label="退出" @click="logout"
            /></el-tooltip>
          </div>
        </header>
        <section v-if="!authenticated" class="login-state">
          <div class="login-symbol"><Terminal :size="30" /></div>
          <h1>设备日志服务</h1>
          <form class="auth" @submit.prevent="login">
            <el-input
              v-model="loginForm.username"
              placeholder="用户名"
              aria-label="用户名"
              autocomplete="username"
            /><el-input
              v-model="loginForm.password"
              type="password"
              placeholder="密码"
              aria-label="密码"
              autocomplete="current-password"
              show-password
            /><el-button
              native-type="submit"
              type="primary"
              :icon="KeyRound"
              :loading="busy"
              >登录</el-button
            >
          </form>
        </section>
        <section v-else-if="!user?.mustChangePassword" class="workspace">
          <el-empty
            v-if="activeTab === 'empty'"
            description="当前账号没有可用的平台权限"
          />
          <div class="page-head">
            <h1>
              {{ navigation.find((item) => item.key === activeTab)?.label }}
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
                  @click="refresh()" /></el-tooltip
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
              }"
              :listeners="{
                edit: editTask,
                view: viewTask,
                changed: () => refresh(),
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
              canControl: hasScope('tasks:control'),
              resourceIds: user?.resourceIds,
            }"
            :listeners="{ tasks: viewResourceTasks }"
          />
          <AsyncView
            v-else-if="activeTab === 'templates'"
            :loader="loadTemplateList"
            :component-props="{
              items: templates,
              canWrite: hasScope('templates:write'),
            }"
            :listeners="{ edit: editTemplate, changed: () => refresh() }"
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
            }"
            :listeners="{
              'update:modelValue': (value: string) => (selectedLogTask = value),
            }"
          />
          <AsyncView
            v-else-if="activeTab === 'access' && user?.isAdmin"
            :loader="loadAccessManager"
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
      </div>
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
      }"
      :listeners="{
        'update:modelValue': (value: boolean) => (taskEditorOpen = value),
        saved: () => refresh(),
      }"
    />
    <AsyncView
      v-if="templateEditorOpen"
      overlay
      :loader="loadTemplateEditor"
      :component-props="{
        modelValue: templateEditorOpen,
        template: selectedTemplate,
      }"
      :listeners="{
        'update:modelValue': (value: boolean) => (templateEditorOpen = value),
        saved: () => refresh(),
      }"
    />
  </el-config-provider>
  <el-dialog
    v-model="passwordOpen"
    title="修改密码"
    width="min(440px,94vw)"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    ><el-form label-position="top"
      ><el-form-item label="当前密码"
        ><el-input
          v-model="passwordForm.current"
          type="password"
          show-password
          autocomplete="current-password" /></el-form-item
      ><el-form-item label="新密码"
        ><el-input
          v-model="passwordForm.next"
          type="password"
          show-password
          autocomplete="new-password" /></el-form-item></el-form
    ><template #footer
      ><el-button v-if="user?.mustChangePassword" @click="logout"
        >退出登录</el-button
      ><el-button
        v-else
        @click="
          passwordOpen = false;
          passwordForm = { current: '', next: '' };
        "
        >取消</el-button
      ><el-button
        type="primary"
        :loading="passwordSaving"
        @click="changePassword"
        >保存新密码</el-button
      ></template
    ></el-dialog
  >
</template>
