<script setup lang="ts">
// 独立日志工作区只保留当前任务快照；选择器分页检索，切换时销毁旧实时连接与作业视图。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Search, RefreshCw } from "lucide-vue-next";
import { api } from "../../shared/api";
import type { Task } from "../../shared/types";
import LiveLogs from "./LiveLogs.vue";
import LogArchives from "./LogArchives.vue";
import CommandHistory from "../commands/CommandHistory.vue";
import LogTaskPicker from "./LogTaskPicker.vue";
import { taskStatusLabel, taskStatusTone } from "../tasks/taskStatus";
import { canManageOwnedRecord } from "../../shared/ownership";
const selected = defineModel<string>({ default: "" });
const props = defineProps<{ canDownload?: boolean; canSend?: boolean; userId?: string; isAdmin?: boolean }>();
const tab = ref("live");
const currentTask = ref<Task>();
const pickerOpen = ref(false);
const refreshing = ref(false);
const stateUnavailable = ref(false);
let stateGeneration = 0;
let stateTimer: number | undefined;

const hasCurrentSnapshot = computed(() => currentTask.value?.id === selected.value);
const canSendCurrent = computed(() => Boolean(
  props.canSend && currentTask.value &&
  canManageOwnedRecord(props.userId, props.isAdmin, currentTask.value.createdBy),
));
const target = computed(() => {
  const task = currentTask.value;
  if (!task) return "";
  const address = task.ip ?? "未知地址";
  return `${address.includes(":") ? `[${address}]` : address}:${task.port ?? "-"}`;
});

// 五秒刷新当前任务；代次和卸载清理阻止旧请求覆盖已切换任务的状态栏。
async function readCurrentTask(current: number) {
  const taskId = selected.value;
  if (!taskId) return;
  refreshing.value = true;
  try {
    const task = await api.task(taskId);
    if (current === stateGeneration && selected.value === task.id) {
      currentTask.value = task;
      stateUnavailable.value = false;
    }
  } catch {
    if (current === stateGeneration) stateUnavailable.value = true;
  } finally {
    if (current === stateGeneration) {
      refreshing.value = false;
      stateTimer = window.setTimeout(() => void readCurrentTask(current), 5000);
    }
  }
}
function resetCurrentTask() {
  window.clearTimeout(stateTimer);
  stateTimer = undefined;
  stateUnavailable.value = false;
  const current = ++stateGeneration;
  if (selected.value) void readCurrentTask(current);
}
function changeTask(task: Task) {
  selected.value = task.id;
  currentTask.value = task;
  tab.value = "live";
}
watch(selected, resetCurrentTask, { immediate: true });
onBeforeUnmount(() => { stateGeneration++; window.clearTimeout(stateTimer); });
</script>
<template>
  <section v-if="selected" class="current-log-task" aria-label="当前日志任务">
    <div class="current-task-details">
      <span class="current-task-label">当前任务</span>
      <template v-if="hasCurrentSnapshot">
        <strong>{{ currentTask?.name }}</strong>
        <span>{{ currentTask?.protocol }}</span>
        <span class="current-task-target">{{ target }}</span>
        <el-tag v-if="!stateUnavailable" size="small" :type="taskStatusTone(currentTask?.status)">{{ taskStatusLabel(currentTask?.status) }}</el-tag>
        <el-tag v-else size="small" type="warning">状态不可用</el-tag>
      </template>
      <span v-else>{{ stateUnavailable ? "任务信息不可用" : "正在读取任务信息" }}</span>
    </div>
    <div class="current-task-actions">
      <el-tooltip content="刷新当前任务状态"><el-button text :icon="RefreshCw" :loading="refreshing" aria-label="刷新当前任务状态" @click="resetCurrentTask" /></el-tooltip>
      <el-button @click="pickerOpen = true">切换任务</el-button>
    </div>
  </section>
  <div v-else class="log-workspace-selector">
    <Search :size="22" />
    <div><strong>未选择采集任务</strong><span>选择任务后查看实时日志、归档和命令记录</span></div>
    <el-button type="primary" @click="pickerOpen = true">选择任务</el-button>
  </div>
  <LogTaskPicker v-model="pickerOpen" @selected="changeTask" />
  <el-tabs v-if="selected" v-model="tab" class="logs-workspace-tabs">
    <el-tab-pane label="实时打印" name="live"><LiveLogs v-if="tab === 'live'" :key="selected" :task-id="selected" :can-send="canSendCurrent" @history="tab = 'archives'" /></el-tab-pane>
    <el-tab-pane label="小时归档与检索" name="archives"><LogArchives v-if="tab === 'archives'" :key="selected" :task-id="selected" :can-download="props.canDownload" /></el-tab-pane>
    <el-tab-pane label="命令记录" name="commands"><CommandHistory v-if="tab === 'commands'" :key="selected" :task-id="selected" /></el-tab-pane>
  </el-tabs>
</template>
<style scoped>
.current-log-task, .log-workspace-selector { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin: 0 0 22px; padding: 12px 0; color: #526467; border-bottom: 1px solid #e4e9ea; }
.current-task-details { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; min-width: 0; }
.current-task-details strong { color: #26393d; overflow-wrap: anywhere; }
.current-task-label { color: #76868a; font-size: 13px; }
.current-task-actions { display: flex; align-items: center; flex-shrink: 0; }
.log-workspace-selector { justify-content: flex-start; padding: 28px 0; color: #76868a; }
.log-workspace-selector > div { display: grid; gap: 4px; min-width: 0; }
.log-workspace-selector strong { color: #34474b; }
.log-workspace-selector span { font-size: 13px; }
.log-workspace-selector .el-button { margin-left: auto; }
.logs-workspace-tabs { min-width: 0; width: 100%; }
.logs-workspace-tabs :deep(.el-tabs__content), .logs-workspace-tabs :deep(.el-tab-pane) { min-width: 0; max-width: 100%; }
@media(max-width: 520px) {
  .current-log-task { align-items: stretch; flex-direction: column; }
  .current-task-actions { justify-content: flex-end; }
  .log-workspace-selector { align-items: flex-start; flex-wrap: wrap; }
  .log-workspace-selector .el-button { margin-left: 38px; }
}
</style>
