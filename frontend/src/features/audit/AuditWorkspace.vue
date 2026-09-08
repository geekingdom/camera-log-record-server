<script setup lang="ts">
// 管理员审计工作区：事件仅按 JSON 文本展示，筛选和分页全部在服务端完成。
import { computed, ref, watch } from "vue";
import { RefreshCw, Search } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import {
  auditApi,
  type AuditEvent,
  type EventPage,
  type QueryFilters,
  type RuntimeEvent,
} from "./api";

type Tab = "audit" | "runtime";
type Row = AuditEvent | RuntimeEvent;

const activeTab = ref<Tab>("audit");
const rows = ref<Row[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(50);
const loading = ref(false);
const denied = ref(false);
const selected = ref<Row>();
const dateRange = ref<[Date, Date]>();
const action = ref("");
const actor = ref("");
const taskId = ref("");
const nodeId = ref("");
const eventType = ref("");

const title = computed(() => activeTab.value === "audit" ? "审计记录" : "运行事件");
const filters = computed<QueryFilters>(() => {
  const range = dateRange.value;
  return {
    start: range?.[0].toISOString(),
    end: range?.[1].toISOString(),
    ...(activeTab.value === "audit"
      ? { action: action.value.trim(), actor: actor.value.trim(), taskId: taskId.value.trim() }
      : { taskId: taskId.value.trim(), nodeId: nodeId.value.trim(), type: eventType.value.trim() }),
  };
});

function formatTime(value: unknown) {
  if (typeof value !== "string") return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
}

function details(row: Row) {
  return JSON.stringify(row, null, 2);
}

async function load() {
  loading.value = true;
  denied.value = false;
  try {
    const result: EventPage<Row> = activeTab.value === "audit"
      ? await auditApi.auditEvents(page.value, pageSize.value, filters.value)
      : await auditApi.runtimeEvents(page.value, pageSize.value, filters.value);
    rows.value = result.items;
    total.value = result.total;
  } catch (error) {
    rows.value = [];
    total.value = 0;
    if (error instanceof Error && "status" in error && error.status === 403) {
      denied.value = true;
      return;
    }
    ElMessage.error(error instanceof Error ? error.message : "读取事件失败");
  } finally {
    loading.value = false;
  }
}

function applyFilters() {
  page.value = 1;
  selected.value = undefined;
  void load();
}

function select(row: Row) {
  selected.value = row;
}

watch(activeTab, () => {
  page.value = 1;
  selected.value = undefined;
  void load();
});
watch([page, pageSize], () => void load());
void load();
</script>

<template>
  <section class="audit-workspace">
    <div class="section-heading">
      <div><h2>{{ title }}</h2><p>仅管理员可查询，事件正文不包含设备日志或口令。</p></div>
      <el-tooltip content="刷新当前记录"><el-button :icon="RefreshCw" aria-label="刷新当前记录" :loading="loading" @click="load" /></el-tooltip>
    </div>
    <el-tabs v-model="activeTab" @tab-change="selected = undefined">
      <el-tab-pane label="审计记录" name="audit" />
      <el-tab-pane label="运行事件" name="runtime" />
    </el-tabs>
    <el-alert v-if="denied" title="当前访问令牌没有管理员权限，无法查看此工作区。" type="error" :closable="false" />
    <template v-else>
      <div class="audit-filters">
        <el-date-picker v-model="dateRange" type="datetimerange" start-placeholder="开始时间" end-placeholder="结束时间" />
        <template v-if="activeTab === 'audit'">
          <el-input v-model="action" placeholder="操作" aria-label="按操作筛选" @keyup.enter="applyFilters" />
          <el-input v-model="actor" placeholder="操作主体" aria-label="按操作主体筛选" @keyup.enter="applyFilters" />
        </template>
        <template v-else>
          <el-input v-model="nodeId" placeholder="节点 ID" aria-label="按节点 ID 筛选" @keyup.enter="applyFilters" />
          <el-input v-model="eventType" placeholder="事件类型" aria-label="按事件类型筛选" @keyup.enter="applyFilters" />
        </template>
        <el-input v-model="taskId" placeholder="任务 ID" aria-label="按任务 ID 筛选" @keyup.enter="applyFilters" />
        <el-button type="primary" :icon="Search" :loading="loading" @click="applyFilters">查询</el-button>
      </div>
      <el-table scrollbar-always-on :data="rows" class="data-table" size="small" v-loading="loading" highlight-current-row @row-click="select">
          <el-table-column label="时间" min-width="170"><template #default="{ row }">{{ formatTime(row.createdAt ?? row.detectedAt) }}</template></el-table-column>
        <template v-if="activeTab === 'audit'">
          <el-table-column prop="action" label="操作" min-width="150" />
          <el-table-column prop="actor" label="主体" min-width="130" />
          <el-table-column prop="targetId" label="目标" min-width="160" />
        </template>
        <template v-else>
          <el-table-column prop="type" label="事件类型" min-width="160" />
          <el-table-column prop="taskId" label="任务" min-width="150" />
          <el-table-column prop="nodeId" label="节点" min-width="150" />
        </template>
      </el-table>
      <el-pagination v-if="total > pageSize" v-model:current-page="page" v-model:page-size="pageSize" :total="total" :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next" />
      <pre v-if="selected" class="event-details">{{ details(selected) }}</pre>
    </template>
  </section>
</template>

<style scoped>
.audit-workspace { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; min-width: 0; }
.audit-workspace > *, .audit-filters > * { min-width: 0; }
.audit-filters :deep(.el-date-editor) { width: 100%; min-width: 0; }
.audit-workspace :deep(.el-pagination) { flex-wrap: wrap; gap: 8px; }
.section-heading { display: flex; justify-content: space-between; gap: 16px; align-items: start; }
.section-heading h2 { margin: 0; }
.section-heading p { margin: 5px 0 0; color: #687478; font-size: 13px; }
.audit-filters { display: grid; grid-template-columns: minmax(240px, 1.5fr) repeat(3, minmax(130px, 1fr)) auto; gap: 8px; }
.event-details { max-height: 280px; overflow: auto; margin: 0; padding: 12px; border: 1px solid #dfe5e7; border-radius: 6px; background: #f7f9f9; color: #263238; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 1000px) { .audit-filters { grid-template-columns: minmax(0, 1fr); } .section-heading { align-items: center; } }
</style>
