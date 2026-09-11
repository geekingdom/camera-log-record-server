<script setup lang="ts">
// 海康资源认证历史仅供审阅；游标分页避免高频历史的总数统计和深页跳过扫描。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api } from "../../shared/api";
import type { AuthenticationRecord, AuthenticationRecordResult, Resource } from "../../shared/types";
import { AuthenticationRecordCursorPager } from "./authenticationRecordPager";

const open = defineModel<boolean>({ required: true });
const props = defineProps<{ resource?: Resource }>();
const result = ref<AuthenticationRecordResult>();
const identityChanged = ref<"true" | "false">();
const range = ref<[Date, Date]>();
const pager = new AuthenticationRecordCursorPager<AuthenticationRecord>();
const pagerRevision = ref(0);
let lifecycleGeneration = 0;

const resultOptions: Array<{ value: AuthenticationRecordResult; label: string }> = [
  { value: "SUCCESS", label: "认证成功" },
  { value: "AUTH_FAILED", label: "认证失败" },
  { value: "OFFLINE", label: "设备离线" },
  { value: "ERROR", label: "认证异常" },
];
const sourceLabels: Record<string, string> = { CREATE: "创建资源", EDIT: "编辑资源", PERIODIC: "周期检查", HEALTH_CHECK: "周期检查", MANUAL: "手动认证" };
const resultLabels: Record<string, string> = Object.fromEntries(resultOptions.map(option => [option.value, option.label]));
const title = computed(() => `${props.resource?.name ?? "设备资源"} · 认证记录`);
const records = computed(() => { pagerRevision.value; return pager.items; });
const page = computed(() => { pagerRevision.value; return pager.currentPage; });
const loading = computed(() => { pagerRevision.value; return pager.loading; });
const hasPrevious = computed(() => { pagerRevision.value; return pager.hasPrevious; });
const hasNext = computed(() => { pagerRevision.value; return pager.hasNext; });

function time(value?: string) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}
function latestTime(record: AuthenticationRecord) { return time(record.latestAt ?? record.createdAt); }
function occurrenceCount(record: AuthenticationRecord) {
  const count = record.occurrenceCount;
  return typeof count === "number" && Number.isFinite(count) && count > 0 ? count : 1;
}
function label(record: AuthenticationRecord, labels: Record<string, string>) { return labels[String(record.result)] ?? record.result; }
function source(record: AuthenticationRecord) { return sourceLabels[record.source ?? ""] ?? record.source ?? "未知来源"; }
function resultTone(value?: string) {
  if (value === "SUCCESS") return "success";
  if (value === "OFFLINE") return "warning";
  if (value === "AUTH_FAILED" || value === "ERROR") return "danger";
  return "info";
}
function identitySummary(record: AuthenticationRecord) {
  if (record.initialAuthentication) return "初始认证";
  if (record.identityChanged) return "设备身份变更";
  return "设备身份未变化";
}
function value(value?: string | null) { return value?.trim() || "未返回"; }
function active(lifecycle: number, resourceId: string) {
  return lifecycle === lifecycleGeneration && open.value && props.resource?.id === resourceId;
}
function touchPager() { pagerRevision.value += 1; }
function filters() {
  return {
    result: result.value,
    start: range.value?.[0].toISOString(),
    end: range.value?.[1].toISOString(),
    identityChanged: identityChanged.value,
  };
}
async function refresh() {
  const resourceId = props.resource?.id;
  if (!resourceId) return;
  const lifecycle = lifecycleGeneration;
  touchPager();
  try {
    await pager.refresh(cursor => api.authenticationRecordsCursor(resourceId, cursor, filters()));
  } catch (error) {
    if (active(lifecycle, resourceId)) {
      ElMessage.error(error instanceof Error ? error.message : "读取认证记录失败");
    }
  } finally {
    touchPager();
  }
}
async function next() {
  const resourceId = props.resource?.id;
  if (!resourceId || !hasNext.value || loading.value) return;
  const lifecycle = lifecycleGeneration;
  touchPager();
  try {
    await pager.next(cursor => api.authenticationRecordsCursor(resourceId, cursor, filters()));
  } catch (error) {
    if (active(lifecycle, resourceId)) {
      ElMessage.error(error instanceof Error ? error.message : "读取下一页认证记录失败");
    }
  } finally {
    touchPager();
  }
}
async function previous() {
  const resourceId = props.resource?.id;
  if (!resourceId || !hasPrevious.value || loading.value) return;
  const lifecycle = lifecycleGeneration;
  touchPager();
  try {
    await pager.previous(cursor => api.authenticationRecordsCursor(resourceId, cursor, filters()));
  } catch (error) {
    if (active(lifecycle, resourceId)) {
      ElMessage.error(error instanceof Error ? error.message : "读取上一页认证记录失败");
    }
  } finally {
    touchPager();
  }
}
function filter() {
  // 新筛选条件不能使用旧条件下的 nextCursor；先失效并清空，再请求新的首屏。
  pager.invalidate();
  touchPager();
  void refresh();
}

watch(() => [open.value, props.resource?.id] as const, ([visible]) => {
  lifecycleGeneration += 1;
  pager.invalidate();
  touchPager();
  if (visible) void refresh();
}, { immediate: true });
onBeforeUnmount(() => { lifecycleGeneration += 1; pager.invalidate(); });
</script>

<template>
  <el-drawer v-model="open" :title="title" size="min(1180px, 96vw)" destroy-on-close>
    <div class="authentication-toolbar" role="search">
      <el-select v-model="result" aria-label="按认证结果筛选" clearable placeholder="全部认证结果" @change="filter">
        <el-option v-for="option in resultOptions" :key="option.value" :label="option.label" :value="option.value" />
      </el-select>
      <el-select v-model="identityChanged" aria-label="按身份变更筛选" clearable placeholder="全部身份状态" @change="filter">
        <el-option label="已变更" value="true" />
        <el-option label="未变更" value="false" />
      </el-select>
      <el-date-picker v-model="range" type="datetimerange" range-separator="至" start-placeholder="认证起始时间" end-placeholder="认证截止时间" @change="filter" />
      <el-button @click="filter">筛选</el-button>
      <el-tooltip content="刷新认证记录"><el-button :icon="RefreshCw" aria-label="刷新认证记录" :loading="loading" @click="refresh" /></el-tooltip>
    </div>
    <el-table :data="records" v-loading="loading" class="data-table authentication-table" empty-text="暂无认证记录">
      <el-table-column label="首次认证（北京时间）" min-width="190"><template #default="{ row }">{{ time(row.createdAt) }}</template></el-table-column>
      <el-table-column label="最近认证（北京时间）" min-width="190"><template #default="{ row }">{{ latestTime(row) }}</template></el-table-column>
      <el-table-column label="次数" width="86" align="right"><template #default="{ row }">{{ occurrenceCount(row) }}</template></el-table-column>
      <el-table-column label="来源" min-width="120"><template #default="{ row }"><el-tag size="small" effect="plain">{{ source(row) }}</el-tag></template></el-table-column>
      <el-table-column label="结果" min-width="120"><template #default="{ row }"><el-tag size="small" :type="resultTone(row.result)">{{ label(row, resultLabels) }}</el-tag></template></el-table-column>
      <el-table-column label="设备身份" min-width="310"><template #default="{ row }"><div class="identity-details"><strong :class="{ changed: row.identityChanged }">{{ identitySummary(row) }}</strong><template v-if="row.identityChanged"><span>型号：{{ value(row.modelBefore) }} → {{ value(row.modelAfter) }}</span><span>序列号：{{ value(row.serialBefore) }} → {{ value(row.serialAfter) }}</span></template><template v-else><span>型号：{{ value(row.modelAfter || row.modelBefore) }}</span><span>序列号：{{ value(row.serialAfter || row.serialBefore) }}</span></template></div></template></el-table-column>
      <el-table-column label="说明" min-width="220"><template #default="{ row }"><span class="authentication-message">{{ row.message || "-" }}</span></template></el-table-column>
    </el-table>
    <div class="authentication-pagination" aria-label="认证记录分页">
      <el-tooltip content="上一页"><el-button :icon="ChevronLeft" circle aria-label="上一页" :disabled="!hasPrevious || loading" @click="previous" /></el-tooltip>
      <span aria-label="当前页">第 {{ page }} 页</span>
      <el-tooltip content="下一页"><el-button :icon="ChevronRight" circle aria-label="下一页" :disabled="!hasNext || loading" @click="next" /></el-tooltip>
      <span class="pagination-size">每页 20 条</span>
    </div>
    <template #footer><el-button @click="open = false">关闭</el-button></template>
  </el-drawer>
</template>

<style scoped>
.authentication-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 14px; }
.authentication-toolbar .el-select { width: min(180px, 100%); }
.identity-details { display: grid; gap: 4px; color: #59676a; overflow-wrap: anywhere; }
.identity-details strong { color: #293d40; }
.identity-details strong.changed { color: #b54708; }
.authentication-message { overflow-wrap: anywhere; }
.authentication-pagination { display: flex; align-items: center; gap: 8px; min-height: 34px; margin-top: 14px; color: #526467; font-size: 13px; }
.authentication-pagination .pagination-size { margin-left: 4px; color: #758588; }
@media (max-width: 620px) {
  .authentication-toolbar .el-select, .authentication-toolbar .el-date-editor { width: 100%; }
}
</style>
