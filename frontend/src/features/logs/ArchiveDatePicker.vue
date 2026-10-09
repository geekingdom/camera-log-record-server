<script setup lang="ts">
// 日历只查询可见月及相邻月的元数据；序号隔离切换任务和快速翻月的迟到响应。
import { onBeforeUnmount, ref, watch } from "vue";
import { api } from "../../shared/api";

const props = defineProps<{ modelValue: string; taskId: string }>();
const emit = defineEmits<{ "update:modelValue": [value: string] }>();
const days = ref(new Set<string>()), loading = ref(false), error = ref(false);
let generation = 0;
async function load(month: string) {
  const current = ++generation;
  days.value = new Set();
  loading.value = true;
  error.value = false;
  try {
    // 日期面板包含前后月份补齐的日期，也一并标记；每次请求都有月界限。
    const [year, number] = month.split("-").map(Number);
    const months = [-1, 0, 1].map(offset => {
      const date = new Date(Date.UTC(year!, number! - 1 + offset, 1));
      return date.toISOString().slice(0, 7);
    });
    const responses = await Promise.all(months.map(value => api.logDays(props.taskId, value)));
    if (current === generation) days.value = new Set(responses.flatMap(response => response.days));
  } catch {
    if (current === generation) error.value = true;
  } finally {
    if (current === generation) loading.value = false;
  }
}
function panelChange(value: Date | Date[]) {
  const date = Array.isArray(value) ? value[0] : value;
  if (date instanceof Date && Number.isFinite(date.getTime())) {
    // Element Plus 的面板日期使用浏览器日历字段，不将它再次转换为上海时刻。
    void load(`${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`);
  }
}
function visibleChange(visible: boolean) {
  if (visible) void load(props.modelValue.slice(0, 7));
}
watch(() => [props.taskId, props.modelValue], () => void load(props.modelValue.slice(0, 7)), { immediate: true });
onBeforeUnmount(() => { generation++; });
</script>

<template>
  <div class="archive-calendar-control">
    <el-date-picker :model-value="modelValue" type="date" value-format="YYYY-MM-DD"
      placeholder="归档日期" aria-label="归档日期（上海时区）" :clearable="false"
      @update:model-value="emit('update:modelValue', $event)" @panel-change="panelChange"
      @visible-change="visibleChange">
      <template #default="cell">
        <div class="el-date-table-cell archive-calendar-cell" :class="{ 'has-logs': days.has(cell.dayjs.format('YYYY-MM-DD')) }"
          :title="`${cell.dayjs.format('YYYY-MM-DD')}${days.has(cell.dayjs.format('YYYY-MM-DD')) ? ' 有日志' : ''}`">
          <span class="el-date-table-cell__text">{{ cell.text }}</span>
          <i v-if="days.has(cell.dayjs.format('YYYY-MM-DD'))" class="archive-calendar-dot" aria-label="有日志" />
        </div>
      </template>
    </el-date-picker>
    <span class="archive-calendar-legend" role="status">
      <template v-if="loading">正在查询日志日期…</template>
      <template v-else-if="error">日期标记加载失败，重新打开日历可重试</template>
      <template v-else><i class="legend-dot" />圆点表示有日志（上海日期）</template>
    </span>
  </div>
</template>

<style scoped>
.archive-calendar-control { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.archive-calendar-legend { display: inline-flex; align-items: center; gap: 6px; color: var(--el-text-color-secondary); font-size: 12px; }
.archive-calendar-cell { position: relative; }
.archive-calendar-cell.has-logs { font-weight: 600; }
.archive-calendar-dot, .legend-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--el-color-success); }
.archive-calendar-dot { position: absolute; left: calc(50% + 9px); top: 4px; box-shadow: 0 0 0 1px var(--el-bg-color-overlay); }
</style>
