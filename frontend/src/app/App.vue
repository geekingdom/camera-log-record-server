<script setup lang="ts">
// 根协调层只保存会话、页签和列表数据；具体编辑器与业务动作下沉到 feature 目录。
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { KeyRound, RefreshCw, Terminal, Plus } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api, clearToken, getToken, setToken } from "../shared/api";
import type { Node, Task, Template } from "../shared/types";
import TaskEditor from "../features/tasks/TaskEditor.vue";
import TemplateEditor from "../features/templates/TemplateEditor.vue";
import TaskList from "../features/tasks/TaskList.vue";
import TemplateList from "../features/templates/TemplateList.vue";
import NodeList from "../features/nodes/NodeList.vue";
const activeTab = ref("tasks"),
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
  selectedTemplate = ref<Template>(),
  taskEditorOpen = ref(false),
  templateEditorOpen = ref(false);
const error = (value: unknown) =>
  ElMessage.error(value instanceof Error ? value.message : "请求失败");
async function loadTasks() {
  const data = await api.tasks(page.value, pageSize.value, {
    search: taskSearch.value.trim() || undefined,
    status: taskStatus.value || undefined,
  });
  tasks.value = data.items;
  totals.value.tasks = data.total;
}
async function loadTemplates() {
  const data = await api.templates(1, 100);
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
    if (activeTab.value === "tasks") await loadTasks();
    else if (activeTab.value === "templates") await loadTemplates();
    else await loadNodes();
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
  clearToken();
  token.value = "";
  authenticated.value = false;
  tasks.value = [];
  templates.value = [];
  nodes.value = [];
  taskEditorOpen.value = false;
  templateEditorOpen.value = false;
}
function createTask() {
  selectedTask.value = undefined;
  taskEditorOpen.value = true;
  void loadTemplates().catch(error);
}
function editTask(task: Task) {
  selectedTask.value = task;
  taskEditorOpen.value = true;
  void loadTemplates().catch(error);
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
let timer: ReturnType<typeof setInterval>;
onMounted(() => {
  if (authenticated.value) void login();
  timer = setInterval(() => void refresh(true), 5000);
});
onBeforeUnmount(() => clearInterval(timer));
</script>
<template>
  <main class="shell">
    <header class="topbar">
      <div class="brand">
        <Terminal :size="21" /><span>设备日志服务</span
        ><small>运行控制台</small>
      </div>
      <form v-if="!authenticated" class="auth" @submit.prevent="login">
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
      <el-button v-else @click="logout">退出</el-button>
    </header>
    <section v-if="!authenticated" class="login-state">
      <KeyRound :size="36" />
      <h1>设备日志服务</h1>
    </section>
    <section v-else class="workspace">
      <el-tabs v-model="activeTab" class="work-tabs"
        ><el-tab-pane name="tasks" label="采集任务" /><el-tab-pane
          name="templates"
          label="命令模板" /><el-tab-pane name="nodes" label="服务节点"
      /></el-tabs>
      <div class="page-head">
        <h1>
          {{
            activeTab === "tasks"
              ? "采集任务"
              : activeTab === "templates"
                ? "命令模板"
                : "服务节点"
          }}
        </h1>
        <div>
          <el-tooltip content="刷新列表"
            ><el-button
              :icon="RefreshCw"
              aria-label="刷新列表"
              @click="refresh()" /></el-tooltip
          ><el-button
            v-if="activeTab !== 'nodes'"
            type="primary"
            :icon="Plus"
            @click="activeTab === 'tasks' ? createTask() : createTemplate()"
            >新建{{ activeTab === "tasks" ? "任务" : "模板" }}</el-button
          >
        </div>
      </div>
      <template v-if="activeTab === 'tasks'"
        ><div class="task-overview">
          <span
            ><b>{{ totals.tasks }}</b> 任务总数</span
          ><span
            ><b>{{
              tasks.filter((task) => task.status === "COLLECTING").length
            }}</b>
            当前页采集</span
          ><span
            ><b>{{
              tasks.filter((task) =>
                ["FAILED", "ERROR", "RECONNECTING"].includes(task.status ?? ""),
              ).length
            }}</b>
            当前页异常</span
          ><span
            ><b>{{ totals.nodes }}</b> 已登记节点</span
          >
        </div>
        <div class="task-filters">
          <el-input
            v-model="taskSearch"
            clearable
            placeholder="搜索任务名称或 IP"
            aria-label="搜索任务名称或 IP"
            @keyup.enter="applyTaskFilters"
          /><el-select
            v-model="taskStatus"
            clearable
            placeholder="全部状态"
            aria-label="按状态筛选"
            @change="applyTaskFilters"
            ><el-option label="采集中" value="COLLECTING" /><el-option
              label="连接中"
              value="CONNECTING" /><el-option
              label="重连中"
              value="RECONNECTING" /><el-option
              label="已暂停"
              value="PAUSED" /><el-option
              label="已停止"
              value="STOPPED" /></el-select
          ><el-button @click="applyTaskFilters">筛选</el-button>
        </div>
        <TaskList
          :items="tasks"
          :loading="busy"
          @edit="editTask"
          @changed="refresh()"
      /></template>
      <TemplateList
        v-else-if="activeTab === 'templates'"
        :items="templates"
        @edit="editTemplate"
        @changed="refresh()"
      />
      <NodeList v-else :items="nodes" :loading="busy" />
      <el-pagination
        v-if="activeTab === 'tasks'"
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="totals.tasks"
        :page-sizes="[20, 50, 100]"
        layout="total, prev, pager, next"
      />
    </section>
  </main>
  <TaskEditor
    v-model="taskEditorOpen"
    :task="selectedTask"
    :templates="templates"
    @saved="refresh()"
  />
  <TemplateEditor
    v-model="templateEditorOpen"
    :template="selectedTemplate"
    @saved="refresh()"
  />
</template>
