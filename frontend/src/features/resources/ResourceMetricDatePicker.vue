<script setup lang="ts">
// 指标日期仅请求可见面板及补齐日期所在月份；代次隔离翻月和资源切换的迟到响应。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { api } from "../../shared/api";
const props = defineProps<{ resourceId: string; anchor: Date }>();
const defaultDates = computed(() => [props.anchor, props.anchor]);
const range = defineModel<[Date, Date] | null>({ required: true });
const days = ref(new Set<string>());
const loading = ref(false), error = ref(false);
let generation = 0;
/** 两侧日期面板可能跨月，分别扩展相邻月份后去重，覆盖日历中的补齐日期。 */
async function load(dates: Date[]) {
  const current = ++generation;
  days.value = new Set(); loading.value = true; error.value = false;
  const months = new Set<string>();
  for (const date of dates) {
    if (!Number.isFinite(date.getTime())) continue;
    for (const offset of [-1, 0, 1]) {
      months.add(new Date(Date.UTC(date.getFullYear(), date.getMonth() + offset, 1)).toISOString().slice(0, 7));
    }
  }
  try {
    const responses = await Promise.all([...months].map(month => api.resourceMetricDates(props.resourceId, month)));
    if (current === generation) days.value = new Set(responses.flatMap(response => response.days));
  } catch {
    if (current === generation) error.value = true;
  } finally {
    if (current === generation) loading.value = false;
  }
}
function panelChange(value: Date | Date[]) { void load(Array.isArray(value) ? value : [value]); }
function visibleChange(visible: boolean) {
  if (visible) {
    const start = range.value?.[0] ?? props.anchor;
    void load([start, range.value?.[1] ?? new Date(start.getFullYear(), start.getMonth() + 1, 1)]);
  } else { generation++; loading.value = false; }
}
watch(() => props.resourceId, () => { generation++; days.value = new Set(); error.value = false; });
onBeforeUnmount(() => { generation++; });
</script>
<template>
  <div class="metrics-calendar-control">
    <el-date-picker v-model="range" type="datetimerange" range-separator="至" start-placeholder="开始时间" end-placeholder="结束时间"
      :default-value="defaultDates" :teleported="false" popper-class="metrics-calendar-popper"
      @panel-change="panelChange" @visible-change="visibleChange">
      <template #default="cell">
        <div class="el-date-table-cell metrics-calendar-cell" :class="{ 'has-metrics': days.has(cell.dayjs.format('YYYY-MM-DD')) }"
          :title="`${cell.dayjs.format('YYYY-MM-DD')}${days.has(cell.dayjs.format('YYYY-MM-DD')) ? ' 有指标采样' : ''}`">
          <span class="el-date-table-cell__text">{{ cell.text }}</span>
          <i v-if="days.has(cell.dayjs.format('YYYY-MM-DD'))" class="metrics-calendar-dot" aria-label="有指标采样" />
        </div>
      </template>
    </el-date-picker>
    <span class="metrics-calendar-legend" role="status">
      <template v-if="loading">正在查询指标日期…</template>
      <template v-else-if="error">日期标记加载失败，重新打开日历可重试</template>
      <template v-else><i class="legend-dot" />圆点表示有指标采样（上海日期）</template>
    </span>
  </div>
</template>
<style scoped>
.metrics-calendar-control { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; min-width: 0; max-width: 100%; }
.metrics-calendar-legend { display: inline-flex; align-items: center; gap: 6px; color: var(--el-text-color-secondary); font-size: 12px; }
.metrics-calendar-cell { position: relative; }
.metrics-calendar-cell.has-metrics { font-weight: 600; }
.metrics-calendar-dot, .legend-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--el-color-success); }
.metrics-calendar-dot { position: absolute; left: calc(50% + 9px); top: 4px; box-shadow: 0 0 0 1px var(--el-bg-color-overlay); }
</style>
<style>
@media (max-width: 767px) {
  .metrics-calendar-popper { position: fixed !important; inset: 8vh auto auto 14px !important; transform: none !important; max-height: 84vh; overflow-y: auto; }
  .metrics-calendar-popper .el-date-range-picker { width: min(646px, calc(100vw - 28px)); }
  .metrics-calendar-popper .el-date-range-picker__content { display: block; width: 100%; }
  .metrics-calendar-popper .el-picker-panel__body { min-width: 0; max-height: calc(84vh - 180px); overflow-y: auto; }
  .metrics-calendar-popper .el-date-range-picker__time-header { display: flex; flex-wrap: wrap; }
}
</style>
