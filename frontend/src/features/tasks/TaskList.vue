<script setup lang="ts">
// 任务列表负责生命周期按钮的可用性与同任务防重复提交，不持有任务详情状态。
import { computed, ref } from "vue";
import { CirclePause, CirclePlay, CircleStop, Edit3, Info } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api } from "../../shared/api";
import type { Task } from "../../shared/types";
import TaskDiagnostics from "./TaskDiagnostics.vue";
import { taskStatusLabel, taskStatusTone } from "./taskStatus";

const props = defineProps<{ items: Task[]; loading: boolean }>();
const emit = defineEmits<{ edit: [Task]; view: [Task]; changed: [] }>();
const pendingIds = ref(new Set<string>());
const activeStatuses = new Set(["COLLECTING", "CONNECTING", "RECONNECTING"]);
const diagnosticId = ref<string>();
const diagnosticsOpen = ref(false);
// 轮询替换任务对象后仍按 ID 选择新快照，不保留打开弹窗时的过期错误。
const diagnosticTask = computed(() => props.items.find(task => task.id === diagnosticId.value));
function showDiagnostics(task: Task) {
  diagnosticId.value = task.id;
  diagnosticsOpen.value = true;
}
const desiredLabels: Record<string, string> = {
  STOPPED: "停止",
  RUNNING: "运行",
  PAUSED: "暂停",
};
const protocolLabels: Record<string, string> = {
  SSH: "SSH",
  TELNET_DEVICE: "Telnet 设备",
  TELNET_SERIAL: "Telnet 串口",
};

function label(value: string | undefined, labels: Record<string, string>) {
  return labels[value ?? ""] ?? value ?? "未知";
}
function busy(task: Task) {
  return pendingIds.value.has(task.id);
}
function canPause(task: Task) {
  return (
    task.protocol === "SSH" &&
    task.status !== "PAUSED" &&
    (activeStatuses.has(task.status ?? "") || task.desiredState === "RUNNING")
  );
}
function canResume(task: Task) {
  return task.protocol === "SSH" && task.status === "PAUSED";
}
function pauseAction(task: Task) {
  return canResume(task) ? "resume" : "pause";
}
function actionName(task: Task) {
  return canResume(task) ? "继续任务" : "暂停任务";
}
// pendingIds 以任务 ID 隔离，避免某一行提交操作时误禁用其它任务。
async function state(
  task: Task,
  action: "start" | "stop" | "pause" | "resume",
) {
  if (busy(task)) return;
  pendingIds.value = new Set(pendingIds.value).add(task.id);
  try {
    await api.operation(task.id, action);
    ElMessage.success(`${task.name} 操作请求已提交`);
    emit("changed");
  } catch (error) {
    const message = error instanceof Error ? error.message : "操作失败";
    ElMessage.error(`${message}（任务 ID: ${task.id}）`);
  } finally {
    const next = new Set(pendingIds.value);
    next.delete(task.id);
    pendingIds.value = next;
  }
}
const rows = computed(() => props.items);
</script>

<template>
  <el-table
    v-loading="props.loading"
    :data="rows"
    class="data-table"
    empty-text="暂无任务"
  >
    <el-table-column label="任务" min-width="220"
      ><template #default="{ row }"
        ><button class="task-name" @click="emit('view', row)" :aria-label="`查看实时打印 · ${row.name}`">{{ row.name }}</button>
        <small class="task-id">ID: {{ row.id }}</small></template
      ></el-table-column
    >
    <el-table-column label="连接" min-width="200"
      ><template #default="{ row }"
        ><el-tag size="small" effect="plain">{{
          protocolLabels[row.protocol] ?? row.protocol
        }}</el-tag
        ><span class="connection-address"
          >{{ row.ip }}:{{ row.port }}</span
        ></template
      ></el-table-column
    >
    <el-table-column label="状态" width="120"
      ><template #default="{ row }"
        ><el-tag
          size="small"
          :type="taskStatusTone(row.status)"
          >{{ taskStatusLabel(row.status) }}</el-tag
        ></template
      ></el-table-column
    >
    <el-table-column label="期望状态" width="120"
      ><template #default="{ row }">{{
        label(row.desiredState, desiredLabels)
      }}</template></el-table-column
    >
    <el-table-column label="操作" width="250" fixed="right"
      ><template #default="{ row }">
        <el-tooltip :content="`查看任务状态 · ID: ${row.id}`">
          <el-button text :icon="Info" aria-label="查看任务状态" @click="showDiagnostics(row)" />
        </el-tooltip>
        <el-tooltip :content="`编辑任务 · ID: ${row.id}`"
          ><el-button
            text
            :icon="Edit3"
            aria-label="编辑任务"
            :disabled="busy(row)"
            @click="emit('edit', row)"
        /></el-tooltip>
        <el-tooltip :content="`启动任务 · ID: ${row.id}`"
          ><el-button
            text
            type="success"
            :icon="CirclePlay"
            aria-label="启动任务"
            :disabled="busy(row) || row.status === 'PAUSED'"
            @click="state(row, 'start')"
        /></el-tooltip>
        <el-tooltip :content="`停止任务 · ID: ${row.id}`"
          ><el-button
            text
            type="danger"
            :icon="CircleStop"
            aria-label="停止任务"
            :disabled="busy(row)"
            @click="state(row, 'stop')"
        /></el-tooltip>
        <el-tooltip
          v-if="canPause(row) || canResume(row)"
          :content="`${actionName(row)} · ID: ${row.id}`"
          ><el-button
            text
            :icon="canResume(row) ? CirclePlay : CirclePause"
            :aria-label="actionName(row)"
            :disabled="busy(row)"
            @click="state(row, pauseAction(row))"
        /></el-tooltip> </template
    ></el-table-column>
  </el-table>
  <TaskDiagnostics v-model="diagnosticsOpen" :task="diagnosticTask" />
</template>
