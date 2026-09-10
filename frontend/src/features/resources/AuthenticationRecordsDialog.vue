<script setup lang="ts">
// 海康资源认证历史仅供审阅；筛选条件变化时以代次隔离迟到的分页响应。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { RefreshCw } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api } from "../../shared/api";
import type { AuthenticationRecord, AuthenticationRecordResult, Resource } from "../../shared/types";

const open = defineModel<boolean>({ required: true });
const props = defineProps<{ resource?: Resource }>();
const records = ref<AuthenticationRecord[]>([]);
const total = ref(0);
const page = ref(1);
const loading = ref(false);
const result = ref<AuthenticationRecordResult>();
const identityChanged = ref<"true" | "false">();
const range = ref<[Date, Date]>();
let lifecycleGeneration = 0;
let listGeneration = 0;

const resultOptions: Array<{ value: AuthenticationRecordResult; label: string }> = [
  { value: "SUCCESS", label: "认证成功" },
  { value: "AUTH_FAILED", label: "认证失败" },
  { value: "OFFLINE", label: "设备离线" },
  { value: "ERROR", label: "认证异常" },
];
const sourceLabels: Record<string, string> = { CREATE: "创建资源", EDIT: "编辑资源", PERIODIC: "周期检查", HEALTH_CHECK: "周期检查", MANUAL: "手动认证" };
const resultLabels: Record<string, string> = Object.fromEntries(resultOptions.map(option => [option.value, option.label]));
const title = computed(() => `${props.resource?.name ?? "设备资源"} · 认证记录`);

function time(value?: string) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
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
async function load() {
  const resourceId = props.resource?.id;
  if (!resourceId) return;
  const current = ++listGeneration;
  const lifecycle = lifecycleGeneration;
  loading.value = true;
  try {
    const data = await api.authenticationRecords(resourceId, page.value, {
      result: result.value,
      start: range.value?.[0].toISOString(),
      end: range.value?.[1].toISOString(),
      identityChanged: identityChanged.value,
    });
    if (current === listGeneration && active(lifecycle, resourceId)) {
      records.value = data.items;
      total.value = data.total;
    }
  } catch (error) {
    if (current === listGeneration && active(lifecycle, resourceId)) {
      ElMessage.error(error instanceof Error ? error.message : "读取认证记录失败");
    }
  } finally {
    if (current === listGeneration) loading.value = false;
  }
}
function filter() { if (page.value === 1) void load(); else page.value = 1; }

watch(page, () => void load());
watch(() => [open.value, props.resource?.id] as const, ([visible]) => {
  lifecycleGeneration += 1;
  listGeneration += 1;
  records.value = [];
  total.value = 0;
  loading.value = false;
  page.value = 1;
  if (visible) void load();
}, { immediate: true });
onBeforeUnmount(() => { lifecycleGeneration += 1; listGeneration += 1; });
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
      <el-tooltip content="刷新认证记录"><el-button :icon="RefreshCw" aria-label="刷新认证记录" @click="load" /></el-tooltip>
    </div>
    <el-table :data="records" v-loading="loading" class="data-table authentication-table" empty-text="暂无认证记录">
      <el-table-column label="认证时间（北京时间）" min-width="190"><template #default="{ row }">{{ time(row.createdAt) }}</template></el-table-column>
      <el-table-column label="来源" min-width="120"><template #default="{ row }"><el-tag size="small" effect="plain">{{ source(row) }}</el-tag></template></el-table-column>
      <el-table-column label="结果" min-width="120"><template #default="{ row }"><el-tag size="small" :type="resultTone(row.result)">{{ label(row, resultLabels) }}</el-tag></template></el-table-column>
      <el-table-column label="设备身份" min-width="310"><template #default="{ row }"><div class="identity-details"><strong :class="{ changed: row.identityChanged }">{{ identitySummary(row) }}</strong><template v-if="row.identityChanged"><span>型号：{{ value(row.modelBefore) }} → {{ value(row.modelAfter) }}</span><span>序列号：{{ value(row.serialBefore) }} → {{ value(row.serialAfter) }}</span></template><template v-else><span>型号：{{ value(row.modelAfter || row.modelBefore) }}</span><span>序列号：{{ value(row.serialAfter || row.serialBefore) }}</span></template></div></template></el-table-column>
      <el-table-column label="说明" min-width="220"><template #default="{ row }"><span class="authentication-message">{{ row.message || "-" }}</span></template></el-table-column>
    </el-table>
    <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="total, prev, pager, next" />
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
@media (max-width: 620px) {
  .authentication-toolbar .el-select, .authentication-toolbar .el-date-editor { width: 100%; }
}
</style>
