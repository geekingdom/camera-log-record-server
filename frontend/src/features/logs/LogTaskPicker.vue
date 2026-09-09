<script setup lang="ts">
// 日志任务选择器只在弹窗打开时请求服务端分页结果，确认前不改变工作台当前任务。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Search } from "lucide-vue-next";
import { api } from "../../shared/api";
import type { Task } from "../../shared/types";
import { taskStatusLabel, taskStatusTone, taskStatusLabels } from "../tasks/taskStatus";
import LogResourceFilter from "./LogResourceFilter.vue";

const open = defineModel<boolean>({ default: false });
const emit = defineEmits<{ selected: [Task] }>();

const pageSize = 10;
const search = ref("");
const status = ref("");
const resourceId = ref("");
const page = ref(1);
const total = ref(0);
const tasks = ref<Task[]>([]);
const pending = ref<Task>();
const loading = ref(false);
let generation = 0;

const hasPendingSelection = computed(() => Boolean(pending.value));
const statuses = computed(() => Object.entries(taskStatusLabels));

// 筛选、翻页和关闭都会推进代次，迟到结果不能重新打开已关闭弹窗的列表。
async function load() {
  const current = ++generation;
  pending.value = undefined;
  loading.value = true;
  try {
    const result = await api.tasks(page.value, pageSize, {
      search: search.value.trim() || undefined,
      status: status.value || undefined,
      resourceId: resourceId.value || undefined,
    });
    if (current !== generation || !open.value) return;
    tasks.value = result.items;
    total.value = result.total;
    pending.value = tasks.value.find(task => task.id === pending.value?.id);
  } catch (error) {
    if (current === generation && open.value) {
      tasks.value = [];
      total.value = 0;
      pending.value = undefined;
      ElMessage.error(error instanceof Error ? error.message : "读取任务失败");
    }
  } finally {
    if (current === generation) loading.value = false;
  }
}

function openPicker(value: boolean) {
  generation += 1;
  pending.value = undefined;
  if (!value) return;
  search.value = "";
  status.value = "";
  resourceId.value = "";
  page.value = 1;
  void load();
}

function changeFilters() {
  page.value = 1;
  pending.value = undefined;
  void load();
}

function changePage(next: number) {
  page.value = next;
  pending.value = undefined;
  void load();
}

function confirmSelection() {
  if (!pending.value) return;
  emit("selected", pending.value);
  open.value = false;
}

watch(open, openPicker);
watch(resourceId, () => { if (open.value) changeFilters(); });
function createdTime(value?: string) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}
onBeforeUnmount(() => { generation += 1; });
</script>

<template>
  <el-dialog
    v-model="open"
    title="选择日志任务"
    width="min(1120px, calc(100% - 24px))"
    class="log-task-picker"
    destroy-on-close
    @closed="generation++"
  >
    <div class="picker-filters">
      <el-input v-model="search" clearable placeholder="搜索任务名称或 IP" aria-label="搜索日志任务" @keyup.enter="changeFilters" @clear="changeFilters">
        <template #prefix><Search :size="16" /></template>
      </el-input>
      <el-select v-model="status" placeholder="全部状态" aria-label="筛选日志任务状态" @change="changeFilters">
        <el-option label="全部状态" value="" />
        <el-option v-for="[value, label] in statuses" :key="value" :label="label" :value="value" />
      </el-select>
      <LogResourceFilter v-model="resourceId" />
      <el-button :icon="Search" :loading="loading" aria-label="搜索日志任务" @click="changeFilters">搜索</el-button>
    </div>
    <div class="picker-table-scroll">
      <el-table
        v-loading="loading"
        :data="tasks"
        row-key="id"
        highlight-current-row
        class="picker-table"
        empty-text="没有符合条件的任务"
        @current-change="pending = $event || undefined"
        @row-click="pending = $event"
      >
        <el-table-column label="选择" width="76">
          <template #default="{ row }">
            <el-radio
              :model-value="pending?.id"
              :value="row.id"
              :aria-label="`选择任务 ${row.name}`"
              @change="pending = row"
            />
          </template>
        </el-table-column>
        <el-table-column label="任务" min-width="210">
          <template #default="{ row }">
            <strong>{{ row.name }}</strong>
            <small class="picker-task-id">ID: {{ row.id }}</small>
          </template>
        </el-table-column>
        <el-table-column label="协议" min-width="120"><template #default="{ row }">{{ row.protocol }}</template></el-table-column>
        <el-table-column label="目标" min-width="180"><template #default="{ row }">{{ row.ip }}:{{ row.port }}</template></el-table-column>
        <el-table-column label="状态" min-width="110">
          <template #default="{ row }"><el-tag size="small" :type="taskStatusTone(row.status)">{{ taskStatusLabel(row.status) }}</el-tag></template>
        </el-table-column>
        <el-table-column label="创建人" min-width="130"><template #default="{ row }">{{ row.createdByName || row.createdBy || '未记录' }}</template></el-table-column>
        <el-table-column label="创建时间（北京时间）" min-width="190"><template #default="{ row }">{{ createdTime(row.createdAt) }}</template></el-table-column>
      </el-table>
    </div>
    <div class="picker-pagination">
      <span>{{ total }} 个任务</span>
      <el-pagination
        background
        layout="prev, pager, next"
        :current-page="page"
        :page-size="pageSize"
        :total="total"
        @current-change="changePage"
      />
    </div>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :disabled="!hasPendingSelection || loading" @click="confirmSelection">确认切换</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.picker-filters { display: grid; grid-template-columns: minmax(0, 1fr) minmax(120px, 140px) minmax(0, 1fr) auto; gap: 10px; margin-bottom: 16px; }
.picker-table-scroll { max-width: 100%; overflow-x: auto; }
.picker-table { min-width: 1020px; }
.picker-task-id { display: block; margin-top: 4px; color: #7b898c; font-size: 11px; overflow-wrap: anywhere; }
.picker-pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-width: 0; margin-top: 16px; color: #728084; font-size: 13px; }
@media (max-width: 760px) {
  .picker-filters { grid-template-columns: minmax(0, 1fr) auto; }
  .picker-filters .el-select { grid-column: 1 / -1; }
  .picker-filters .resource-filter { grid-column: 1 / -1; }
  .picker-pagination { align-items: flex-start; flex-direction: column; }
  .picker-pagination :deep(.el-pagination) { max-width: 100%; overflow-x: auto; }
}
</style>
