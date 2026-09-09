<script setup lang="ts">
// 管理员排障工作区：三类事件共享筛选、分页和会话边界，详情优先呈现可行动的关联信息。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { RefreshCw, RotateCcw, Search, SlidersHorizontal } from "lucide-vue-next";
import { auditApi, type AuditEvent, type EventBase, type EventLevel, type EventOutcome, type EventPage, type QueryFilters, type RequestEvent, type RuntimeEvent } from "./api";
import AuditEventDrawer from "./AuditEventDrawer.vue";

type Tab = "audit" | "runtime" | "request";
type Row = AuditEvent | RuntimeEvent | RequestEvent;

const activeTab = ref<Tab>("audit");
const rows = ref<Row[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(50);
const loading = ref(false);
const denied = ref(false);
const error = ref("");
const selected = ref<Row>();
const detailOpen = ref(false);
const dateRange = ref<[Date, Date]>();
const primary = ref("");
const taskId = ref("");
const actor = ref("");
const nodeId = ref("");
const level = ref<EventLevel>();
const outcome = ref<EventOutcome>();
const requestId = ref("");
const clientIp = ref("");
const route = ref("");
const status = ref("");
let generation = 0;

const tabTitle = computed(() => ({ audit: "审计记录", runtime: "运行事件", request: "请求记录" })[activeTab.value]);
const primaryLabel = computed(() => ({ audit: "操作", runtime: "事件类型", request: "请求方法" })[activeTab.value]);
const primaryPlaceholder = computed(() => ({ audit: "例如 control:PAUSED", runtime: "例如 CONNECTION_GAP", request: "例如 POST" })[activeTab.value]);
const drawerTitle = computed(() => `${tabTitle.value}详情`);
const hasRows = computed(() => rows.value.length > 0);
const filters = computed<QueryFilters>(() => {
  const range = dateRange.value;
  const shared = {
    start: range?.[0].toISOString(), end: range?.[1].toISOString(),
    level: level.value, requestId: requestId.value.trim(),
  };
  if (activeTab.value === "audit") return {
    ...shared, action: primary.value.trim(), actor: actor.value.trim(), taskId: taskId.value.trim(), outcome: outcome.value,
  };
  if (activeTab.value === "runtime") return {
    ...shared, type: primary.value.trim(), taskId: taskId.value.trim(), nodeId: nodeId.value.trim(), outcome: outcome.value,
  };
  return {
    ...shared, method: primary.value.trim(), taskId: taskId.value.trim(), route: route.value.trim(), status: status.value.trim(), clientIp: clientIp.value.trim(), outcome: outcome.value,
  };
});

function formatTime(value: unknown) {
  if (typeof value !== "string") return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
}

function rowTime(row: EventBase) {
  return formatTime(row.createdAt ?? row.detectedAt);
}

function levelType(value?: EventLevel) {
  return value === "ERROR" ? "danger" : value === "WARNING" ? "warning" : "success";
}

function outcomeType(value?: EventOutcome) {
  return value === "FAILED" ? "danger" : value === "PENDING" || value === "UNKNOWN" ? "warning" : value === "CANCELLED" ? "info" : "success";
}

function levelLabel(value?: EventLevel) {
  return ({ INFO: "信息", WARNING: "警告", ERROR: "错误" } as const)[value ?? "INFO"];
}

function outcomeLabel(value?: EventOutcome) {
  return ({ SUCCEEDED: "成功", FAILED: "失败", PENDING: "处理中", CANCELLED: "已取消", UNKNOWN: "未知" } as const)[value ?? "UNKNOWN"];
}

function summary(row: Row) {
  if (row.summary) return row.summary;
  if (activeTab.value === "audit") return (row as AuditEvent).action || "未命名审计操作";
  if (activeTab.value === "runtime") return (row as RuntimeEvent).type || "未命名运行事件";
  const request = row as RequestEvent;
  return [request.method, request.route].filter(Boolean).join(" ") || "未命名请求";
}

function target(row: Row) {
  if (activeTab.value === "request") return (row as RequestEvent).route || "-";
  return row.targetName || row.taskName || row.targetId || row.taskId || "-";
}

function actorValue(row: Row) {
  return (row as AuditEvent | RequestEvent).actorName || (row as AuditEvent | RequestEvent).actor || "未记录";
}

async function load() {
  const current = ++generation;
  const tab = activeTab.value;
  loading.value = true;
  denied.value = false;
  error.value = "";
  try {
    const result: EventPage<Row> = tab === "audit"
      ? await auditApi.auditEvents(page.value, pageSize.value, filters.value)
      : tab === "runtime"
        ? await auditApi.runtimeEvents(page.value, pageSize.value, filters.value)
        : await auditApi.requestEvents(page.value, pageSize.value, filters.value);
    if (current !== generation) return;
    rows.value = result.items;
    total.value = result.total;
  } catch (reason) {
    if (current !== generation) return;
    rows.value = [];
    total.value = 0;
    if (reason instanceof Error && "status" in reason && reason.status === 403) denied.value = true;
    else error.value = reason instanceof Error ? reason.message : "读取事件失败";
  } finally {
    if (current === generation) loading.value = false;
  }
}

function applyFilters() {
  selected.value = undefined;
  detailOpen.value = false;
  if (page.value !== 1) page.value = 1;
  else void load();
}

function resetFilters() {
  dateRange.value = undefined;
  primary.value = "";
  taskId.value = "";
  actor.value = "";
  nodeId.value = "";
  level.value = undefined;
  outcome.value = undefined;
  requestId.value = "";
  clientIp.value = "";
  route.value = "";
  status.value = "";
  applyFilters();
}

function select(row: Row) {
  selected.value = row;
  detailOpen.value = true;
}

function setRange(hours: number) {
  dateRange.value = [new Date(Date.now() - hours * 60 * 60 * 1000), new Date()];
  applyFilters();
}

watch(activeTab, () => {
  primary.value = "";
  applyFilters();
});
watch([page, pageSize], () => void load());
onBeforeUnmount(() => { generation += 1; });
void load();
</script>

<template>
  <section class="audit-workspace">
    <header class="audit-header">
      <div>
        <div class="audit-kicker"><SlidersHorizontal :size="15" /> 事件排障</div>
        <h2>{{ tabTitle }}</h2>
        <p>按追踪编号、执行结果和关联对象定位平台行为。</p>
      </div>
      <el-tooltip content="刷新当前事件列表"><el-button :icon="RefreshCw" aria-label="刷新当前事件列表" :loading="loading" @click="load" /></el-tooltip>
    </header>

    <el-tabs v-model="activeTab" class="audit-tabs">
      <el-tab-pane label="审计记录" name="audit" />
      <el-tab-pane label="运行事件" name="runtime" />
      <el-tab-pane label="请求记录" name="request" />
    </el-tabs>

    <el-alert v-if="denied" title="当前账号没有管理员查询权限。" type="error" :closable="false" show-icon />
    <template v-else>
      <section class="audit-filter-panel" aria-label="事件筛选">
        <div class="range-tools">
          <el-date-picker v-model="dateRange" type="datetimerange" start-placeholder="开始时间" end-placeholder="结束时间" aria-label="按时间范围筛选" />
          <el-button-group><el-button size="small" @click="setRange(1)">近 1 小时</el-button><el-button size="small" @click="setRange(24)">近 24 小时</el-button><el-button size="small" @click="setRange(168)">近 7 天</el-button></el-button-group>
        </div>
        <div class="audit-filters">
          <el-input v-model="primary" :placeholder="primaryPlaceholder" :aria-label="`按${primaryLabel}筛选`" @keyup.enter="applyFilters"><template #prepend>{{ primaryLabel }}</template></el-input>
          <el-select v-model="level" clearable placeholder="级别" aria-label="按级别筛选"><el-option label="信息" value="INFO" /><el-option label="警告" value="WARNING" /><el-option label="错误" value="ERROR" /></el-select>
          <el-select v-model="outcome" clearable placeholder="结果" aria-label="按结果筛选"><el-option label="成功" value="SUCCEEDED" /><el-option label="失败" value="FAILED" /><el-option label="处理中" value="PENDING" /><el-option label="已取消" value="CANCELLED" /><el-option label="未知" value="UNKNOWN" /></el-select>
          <el-input v-model="requestId" placeholder="请求编号" aria-label="按请求编号筛选" @keyup.enter="applyFilters" />
          <el-input v-if="activeTab === 'audit'" v-model="actor" placeholder="操作者 ID" aria-label="按操作者 ID 筛选" @keyup.enter="applyFilters" />
          <el-input v-model="taskId" placeholder="任务 ID" aria-label="按任务 ID 筛选" @keyup.enter="applyFilters" />
          <el-input v-if="activeTab === 'runtime'" v-model="nodeId" placeholder="节点 ID" aria-label="按节点 ID 筛选" @keyup.enter="applyFilters" />
          <el-input v-if="activeTab === 'request'" v-model="route" placeholder="路由" aria-label="按路由筛选" @keyup.enter="applyFilters" />
          <el-input v-if="activeTab === 'request'" v-model="status" placeholder="状态码" aria-label="按状态码筛选" @keyup.enter="applyFilters" />
          <el-input v-if="activeTab === 'request'" v-model="clientIp" placeholder="来源 IP" aria-label="按来源 IP 筛选" @keyup.enter="applyFilters" />
        </div>
        <div class="filter-actions"><el-button :icon="RotateCcw" @click="resetFilters">重置</el-button><el-button type="primary" :icon="Search" :loading="loading" @click="applyFilters">查询</el-button></div>
      </section>

      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="audit-error" />
      <el-table v-loading="loading" :data="rows" class="data-table audit-table" scrollbar-always-on highlight-current-row empty-text="当前条件下没有事件" @row-click="select">
        <el-table-column label="事件摘要" min-width="245" show-overflow-tooltip><template #default="{ row }"><div class="summary-cell"><strong>{{ summary(row) }}</strong><small v-if="row.reason">{{ row.reason }}</small></div></template></el-table-column>
        <el-table-column label="级别 / 结果" min-width="132"><template #default="{ row }"><div class="tag-stack"><el-tooltip :content="row.level || '未记录'"><el-tag :type="levelType(row.level)" effect="plain">{{ row.level ? levelLabel(row.level) : "未记录" }}</el-tag></el-tooltip><el-tooltip v-if="row.outcome" :content="row.outcome"><el-tag :type="outcomeType(row.outcome)" effect="plain">{{ outcomeLabel(row.outcome) }}</el-tag></el-tooltip><el-tag v-else-if="activeTab === 'request'" :type="Number(row.httpStatus) >= 500 ? 'danger' : Number(row.httpStatus) >= 400 ? 'warning' : 'success'" effect="plain">{{ row.httpStatus || "未记录" }}</el-tag></div></template></el-table-column>
        <el-table-column label="对象" min-width="180" show-overflow-tooltip><template #default="{ row }"><div class="object-cell"><strong>{{ target(row) }}</strong><small>{{ row.deviceIp || row.taskId || row.nodeId || "未记录" }}</small></div></template></el-table-column>
        <el-table-column label="操作者 / 来源" min-width="168"><template #default="{ row }"><div class="object-cell"><strong>{{ actorValue(row) }}</strong><small>{{ row.clientIp || "未记录" }}</small></div></template></el-table-column>
        <el-table-column label="时间" min-width="178"><template #default="{ row }"><span class="event-time">{{ rowTime(row) }}</span><small v-if="row.requestId" class="request-short">{{ row.requestId }}</small></template></el-table-column>
      </el-table>
      <div v-if="hasRows || total > pageSize" class="audit-pagination"><span>{{ total }} 条记录</span><el-pagination v-model:current-page="page" v-model:page-size="pageSize" :total="total" :page-sizes="[20, 50, 100]" layout="sizes, prev, pager, next" /></div>
    </template>

    <AuditEventDrawer v-model="detailOpen" :row="selected" :title="drawerTitle" />
  </section>
</template>

<style scoped>
.audit-workspace { display: grid; gap: 16px; min-width: 0; }
.audit-header { display: flex; align-items: start; justify-content: space-between; gap: 16px; }
.audit-kicker { display: flex; align-items: center; gap: 6px; color: #52706d; font-size: 12px; font-weight: 600; }
.audit-header h2 { margin: 5px 0 0; color: #26383b; }
.audit-header p { margin: 5px 0 0; color: #718085; font-size: 13px; }
.audit-tabs { margin-top: -5px; }
.audit-filter-panel { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px 12px; padding: 12px; border: 1px solid #e2e8e9; border-radius: 6px; background: #fbfcfc; }
.range-tools { display: flex; align-items: center; gap: 8px; min-width: 0; }
.range-tools :deep(.el-date-editor) { width: min(440px, 100%); }
.audit-filters { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 8px; }
.filter-actions { display: flex; align-items: center; justify-content: end; gap: 8px; }
.audit-error { margin-top: -4px; }
.summary-cell, .object-cell { display: grid; gap: 3px; min-width: 0; }
.summary-cell strong, .object-cell strong { overflow: hidden; color: #2b3e42; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.summary-cell small, .object-cell small, .request-short { overflow: hidden; color: #78878b; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.tag-stack { display: flex; flex-wrap: wrap; gap: 5px; }
.event-time { display: block; color: #3e5155; font-variant-numeric: tabular-nums; white-space: nowrap; }
.request-short { display: block; max-width: 160px; margin-top: 4px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.audit-pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: #718085; font-size: 12px; }
.audit-pagination :deep(.el-pagination) { flex-wrap: wrap; justify-content: end; gap: 6px; }
@media (max-width: 1050px) { .audit-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 700px) { .audit-header { align-items: center; } .audit-header p { display: none; } .audit-filter-panel { grid-template-columns: 1fr; } .range-tools { align-items: stretch; flex-direction: column; } .range-tools :deep(.el-date-editor) { width: 100%; } .filter-actions { justify-content: stretch; } .filter-actions .el-button { flex: 1; } .audit-filters { grid-template-columns: minmax(0, 1fr); } .audit-pagination { align-items: start; flex-direction: column; } .audit-pagination :deep(.el-pagination) { justify-content: start; } }
</style>
