<script setup lang="ts">
// 检索索引只作为候选加速提示；范围变化时独立读取，任何未覆盖情况都不能被解释为无日志。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Database, RefreshCw } from "lucide-vue-next";
import { api } from "../../shared/api";
import type { SearchIndexStatus } from "../../shared/types";
import { shanghaiDateTimeToUtc } from "./archiveDate";

const props = defineProps<{ taskId: string; start: string; end: string }>();
const status = ref<SearchIndexStatus>();
const loading = ref(false);
let generation = 0;
function shanghaiTime(value?: string | null) {
  if (!value) return "暂无";
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? value : time.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}
const availability = computed(() => {
  const value = status.value;
  if (!value || value.status === "UNAVAILABLE") return { type: "warning" as const, text: "索引暂不可用" };
  if (!value.enabled || value.status === "DISABLED") return { type: "info" as const, text: "索引未启用" };
  return { type: value.coverageComplete ? "success" as const : "warning" as const, text: value.coverageComplete ? "查询范围连续覆盖" : "部分覆盖" };
});
// 任务、开始和结束任一变化均使上个请求失效，避免搜索范围的迟到状态覆盖当前范围。
async function load() {
  const current = ++generation;
  loading.value = true;
  try {
    const result = await api.searchIndexStatus(props.taskId, shanghaiDateTimeToUtc(props.start), shanghaiDateTimeToUtc(props.end));
    if (current === generation) status.value = result;
  } catch {
    if (current === generation) status.value = { enabled: false, status: "UNAVAILABLE", totalFiles: 0, coveredFiles: 0, coverageComplete: false, fallback: "RAW_SCAN" };
  } finally {
    if (current === generation) loading.value = false;
  }
}
watch(() => [props.taskId, props.start, props.end], () => void load(), { immediate: true });
onBeforeUnmount(() => { generation++; });
</script>
<template>
  <aside class="search-index-coverage" aria-label="检索索引覆盖状态">
    <div class="index-heading"><span><Database :size="15" />索引覆盖状态</span><el-button text :icon="RefreshCw" :loading="loading" aria-label="刷新索引覆盖状态" @click="load" /></div>
    <div class="index-content"><el-tag size="small" effect="plain" :type="availability.type">{{ availability.text }}</el-tag><template v-if="status?.enabled && status.status === 'AVAILABLE'"><span>已覆盖 {{ status.coveredFiles }}/{{ status.totalFiles }} 个分卷</span><span v-if="status.coverageComplete">查询范围已连续覆盖。</span><span v-else>连续覆盖至北京时间 {{ shanghaiTime(status.contiguousCoveredThrough) }}。</span><span>最近已索引小时：北京时间 {{ shanghaiTime(status.latestIndexedHour) }}<template v-if="!status.coverageComplete">（不代表连续覆盖）</template>。</span><span v-if="status.truncated">目录范围已截断，未列入范围仍会扫描原文。</span></template><span v-else-if="status?.status === 'DISABLED'">服务未启用索引，本次检索将扫描原文。</span><span v-else>索引不可用或查询范围未覆盖时会回退原文扫描；这不表示没有日志。</span><span v-if="status?.fallback === 'RAW_SCAN'">未覆盖的分卷仍会按原文确认结果。</span></div>
  </aside>
</template>
<style scoped>
.search-index-coverage { display: grid; gap: 8px; margin: 12px 0; padding: 10px 12px; border-left: 2px solid #b8dbd4; background: #f6faf9; color: #617276; font-size: 12px; line-height: 1.6; }
.index-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: #3d5558; font-weight: 600; }.index-heading span { display: inline-flex; align-items: center; gap: 6px; }.index-content { display: flex; flex-wrap: wrap; gap: 4px 12px; }.index-content span { overflow-wrap: anywhere; }
</style>
