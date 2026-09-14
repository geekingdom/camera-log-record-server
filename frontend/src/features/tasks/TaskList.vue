<script setup lang="ts">
// 任务列表负责生命周期按钮的可用性与同任务防重复提交，不持有任务详情状态。
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { CirclePause, CirclePlay, CircleStop, Edit3, Info, RotateCcw } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api } from "../../shared/api";
import { confirmAction } from "../../shared/confirm";
import type { Task } from "../../shared/types";
import AsyncView from "../../shared/AsyncView.vue";
import { applicableTaskActions, availableTaskActions, type TaskAction } from "./taskActions";
import { taskStatusLabel, taskStatusTone } from "./taskStatus";
import { canManageOwnedRecord } from "../../shared/ownership";
import { runSequentially, type BulkOperationEntry } from "../../shared/bulkOperations";
import CreatorFilter from "../../shared/CreatorFilter.vue";
import BlockedRestartDialog from "./BlockedRestartDialog.vue";
import { sshTargetLabel } from "./sshTarget";

const loadTaskDiagnostics = () => import("./TaskDiagnostics.vue");

const props = defineProps<{ items: Task[]; loading: boolean; canWrite?: boolean; canControl?: boolean; userId?: string; isAdmin?: boolean; createdBy?: string; showAll?: boolean; selectionKey?: string }>();
const emit = defineEmits<{ edit: [Task]; view: [Task]; changed: []; filters: [filters: { createdBy: string; showAll: boolean }] }>();
const pendingIds = ref(new Set<string>());
const diagnosticId = ref<string>();
const diagnosticsOpen = ref(false);
const blockedRestartTask = ref<Task>();
const selected = ref<Task[]>([]), selectedIds = ref(new Set<string>()), applying = ref(false), results = ref<BulkOperationEntry[]>([]);
const table = ref<{ clearSelection: () => void; toggleRowSelection: (row: Task, selected?: boolean) => void }>();
let pageGeneration = 0, mounted = true, restoringSelection = false, selectionInteraction = false;
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
  WAITING_DEVICE: "等待设备",
};
const protocolLabels: Record<string, string> = {
  SSH: "SSH",
  TELNET_DEVICE: "Telnet 设备",
  TELNET_SERIAL: "Telnet 串口",
};
const taskActions: Exclude<TaskAction, "restart">[] = ["start", "stop", "pause", "resume"];

function label(value: string | undefined, labels: Record<string, string>) {
  return labels[value ?? ""] ?? value ?? "未知";
}
function busy(task: Task) {
  return pendingIds.value.has(task.id);
}
function owns(task: Task) {
  return canManageOwnedRecord(props.userId, props.isAdmin, task.createdBy);
}
function showsAction(task: Task, action: TaskAction) {
  return !task.resourceDeleted && availableTaskActions(task).includes(action);
}
function selectable(task?: Task) { return Boolean(task && props.canControl && owns(task)); }
function clearSelection() { selected.value = []; selectedIds.value = new Set(); table.value?.clearSelection(); }
function updateSelection(items: Task[]) {
  if (restoringSelection) return;
  const next = items.filter(selectable);
  if (!next.length && selectedIds.value.size && !selectionInteraction) return;
  selected.value = next;
  selectedIds.value = new Set(selected.value.map(item => item.id));
  selectionInteraction = false;
}
function markSelectionInteraction() { selectionInteraction = true; }
const pageIds = computed(() => props.items.map(item => item.id).join("\u0000"));
watch(pageIds, () => { pageGeneration += 1; clearSelection(); });
watch(() => props.selectionKey, () => { pageGeneration += 1; clearSelection(); });
watch(() => props.items, async items => {
  const ids = new Set(selectedIds.value);
  const current = items.filter(item => ids.has(item.id) && selectable(item));
  if (current.length === selected.value.length && current.every((item, index) => item === selected.value[index])) return;
  selected.value = current;
  selectedIds.value = new Set(current.map(item => item.id));
  await nextTick();
  restoringSelection = true;
  current.forEach(item => table.value?.toggleRowSelection(item, true));
  await nextTick();
  setTimeout(() => { restoringSelection = false; }, 0);
});
function applicable(action: Exclude<TaskAction, "restart">) { return applicableTaskActions(selected.value, action); }
function actionLabel(action: TaskAction) { return ({ start: "启动", restart: "重新启动", stop: "停止", pause: "暂停", resume: "继续" } as const)[action]; }
function openBlockedRestart(task: Task) {
  if (!props.canControl || !owns(task) || busy(task) || !showsAction(task, "restart")) return;
  blockedRestartTask.value = task;
}
async function applySelected(action: Exclude<TaskAction, "restart">) {
  if (applying.value || !selected.value.length) return;
  const generation = pageGeneration;
  const classified = applicable(action);
  if (!classified.applicable.length) return ElMessage.warning(`已选任务中没有可${actionLabel(action)}的任务，已跳过 ${classified.skipped.length} 项`);
  const frozenApplicable = classified.applicable.map(({ id, name, version }) => ({ id, name, version }));
  const frozenSkipped = classified.skipped.map(({ id, name }) => ({ id, name }));
  if (!await confirmAction(`将提交${actionLabel(action)}请求：适用 ${frozenApplicable.length} 项，跳过 ${frozenSkipped.length} 项。任务状态会由节点异步完成。`, "确认批量任务操作")) return;
  if (!mounted || pageGeneration !== generation) return;
  applying.value = true; results.value = [];
  const skipped = frozenSkipped.map(item => ({ ...item, status: "skipped" as const }));
  const submitted = await runSequentially(frozenApplicable, async (id) => { await api.operation(id, action); }, {
    shouldContinue: () => mounted && pageGeneration === generation,
    onProgress: entries => { results.value = [...skipped, ...entries]; },
  });
  results.value = [...skipped, ...submitted]; applying.value = false;
  const retained = new Set(results.value.filter(item => item.status !== "success").map(item => item.id));
  selected.value = props.items.filter(item => retained.has(item.id) && selectable(item));
  selectedIds.value = new Set(selected.value.map(item => item.id));
  await nextTick(); table.value?.clearSelection(); selected.value.forEach(item => table.value?.toggleRowSelection(item, true));
  if (submitted.some(item => item.status === "success")) emit("changed");
}
function applyFilters(change: Partial<{ createdBy: string; showAll: boolean }>) { emit("filters", { createdBy: props.createdBy || "", showAll: Boolean(props.showAll), ...change }); }
onBeforeUnmount(() => { mounted = false; pageGeneration += 1; });
// pendingIds 以任务 ID 隔离，避免某一行提交操作时误禁用其它任务。
async function state(
  task: Task,
  action: "start" | "stop" | "pause" | "resume",
) {
  if (!props.canControl || !owns(task) || busy(task)) return;
  if (!await confirmAction(`确认${action === "start" ? "启动" : action === "stop" ? "停止" : action === "pause" ? "暂停" : "继续"}任务“${task.name}”吗？`, "确认任务操作")) return;
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
  <div class="table-actions"><CreatorFilter v-if="props.showAll" :model-value="props.createdBy || ''" @change="(value: string) => applyFilters({ createdBy: value })" /><el-checkbox :model-value="Boolean(props.showAll)" @change="(value: boolean | string | number) => applyFilters({ showAll: Boolean(value) })">查看全部</el-checkbox><el-button v-for="action in taskActions" :key="action" :disabled="!selected.length || applying" :loading="applying" @click="applySelected(action)">批量{{ actionLabel(action) }} ({{ applicable(action).applicable.length }}/{{ selected.length }})</el-button></div>
  <div v-if="results.length" class="bulk-operation-result" aria-live="polite"><p>已提交 {{ results.filter(item => item.status === 'success').length }} 项控制请求；失败 {{ results.filter(item => item.status === 'error').length }} 项；跳过 {{ results.filter(item => item.status === 'skipped').length }} 项。</p><ul><li v-for="item in results" :key="item.id">{{ item.name }}：{{ item.status === 'success' ? '已提交控制请求' : item.status === 'error' ? `失败${item.error ? `（${item.error}）` : ''}` : '跳过' }}</li></ul></div>
  <el-table
    ref="table"
    scrollbar-always-on
    v-loading="props.loading"
    :data="rows"
    row-key="id"
    reserve-selection
    @pointerdown.capture="markSelectionInteraction"
    @selection-change="updateSelection"
    class="data-table"
    empty-text="暂无任务"
  >
    <el-table-column type="selection" width="48" :selectable="selectable" />
    <el-table-column label="任务" min-width="220"
      ><template #default="{ row }"
        ><button class="task-name" @click="emit('view', row)" :aria-label="`查看实时打印 · ${row.name}`">{{ row.name }}</button>
        <small class="task-id">创建用户：{{ row.createdByName || row.createdBy || "未知" }} · ID: {{ row.id }}</small></template
      ></el-table-column
    >
    <el-table-column label="连接" min-width="200"
      ><template #default="{ row }"
        ><el-tag size="small" effect="plain">{{
          protocolLabels[row.protocol] ?? row.protocol
        }}<template v-if="row.protocol === 'SSH'"> · {{ sshTargetLabel(row.sshTarget) }}</template></el-tag
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
    <el-table-column label="创建时间" min-width="180"><template #default="{ row }">{{ row.createdAt ? new Date(row.createdAt).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '-' }}</template></el-table-column>
    <el-table-column label="资源删除时间" min-width="180"><template #default="{ row }">{{ row.resourceDeletedAt ? new Date(row.resourceDeletedAt).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '-' }}</template></el-table-column>
    <el-table-column label="操作" width="250" fixed="right"
      ><template #default="{ row }">
        <el-tooltip :content="`查看任务状态 · ID: ${row.id}`">
          <el-button text :icon="Info" aria-label="查看任务状态" @click="showDiagnostics(row)" />
        </el-tooltip>
        <el-tooltip v-if="props.canWrite && owns(row) && !row.resourceDeleted" :content="`编辑任务 · ID: ${row.id}`"
          ><el-button
            text
            :icon="Edit3"
            aria-label="编辑任务"
            :disabled="busy(row)"
            @click="emit('edit', row)"
        /></el-tooltip>
        <el-tooltip v-if="props.canControl && owns(row) && showsAction(row, 'start')" :content="`启动任务 · ID: ${row.id}`"
          ><el-button
            text
            type="success"
            :icon="CirclePlay"
            aria-label="启动任务"
            :disabled="busy(row)"
            @click="state(row, 'start')"
        /></el-tooltip>
        <el-tooltip v-if="props.canControl && owns(row) && showsAction(row, 'restart')" :content="`重新启动任务 · ID: ${row.id}`"
          ><el-button
            text
            type="warning"
            :icon="RotateCcw"
            aria-label="重新启动任务"
            :disabled="busy(row)"
            @click="openBlockedRestart(row)"
        /></el-tooltip>
        <el-tooltip v-if="props.canControl && owns(row) && showsAction(row, 'stop')" :content="`停止任务 · ID: ${row.id}`"
          ><el-button
            text
            type="danger"
            :icon="CircleStop"
            aria-label="停止任务"
            :disabled="busy(row)"
            @click="state(row, 'stop')"
        /></el-tooltip>
        <el-tooltip v-if="props.canControl && owns(row) && showsAction(row, 'pause')" :content="`暂停任务 · ID: ${row.id}`"
          ><el-button
            text
            :icon="CirclePause"
            aria-label="暂停任务"
            :disabled="busy(row)"
            @click="state(row, 'pause')"
        /></el-tooltip>
        <el-tooltip v-if="props.canControl && owns(row) && showsAction(row, 'resume')" :content="`继续任务 · ID: ${row.id}`"
          ><el-button
            text
            :icon="CirclePlay"
            aria-label="继续任务"
            :disabled="busy(row)"
            @click="state(row, 'resume')"
        /></el-tooltip> </template
    ></el-table-column>
  </el-table>
  <AsyncView
    v-if="diagnosticsOpen"
    overlay
    :loader="loadTaskDiagnostics"
    :component-props="{ modelValue: diagnosticsOpen, task: diagnosticTask }"
    :listeners="{ 'update:modelValue': (value: boolean) => diagnosticsOpen = value }"
  />
  <BlockedRestartDialog
    v-if="blockedRestartTask"
    v-model="blockedRestartTask"
    :is-admin="props.isAdmin"
    @submitted="emit('changed')"
  />
</template>
