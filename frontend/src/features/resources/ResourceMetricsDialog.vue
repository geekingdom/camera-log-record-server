<script setup lang="ts">
// 资源监控历史按游标完整读取；图表仅渲染服务端已脱敏的结构化指标。
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Download, Pause, Play, RefreshCw } from "lucide-vue-next";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { api } from "../../shared/api";
import type { Resource, ResourceMetricSample } from "../../shared/types";
import { metricIssueCount, metricSeries, metricSummaries, metricsCsv, validMetricRange } from "./resourceMetrics";
import { memoryPrecision, memoryUnit, metricAxis } from "./metricAxis";
import { relativeMetricSeries } from "./metricRelative";

echarts.use([LineChart, DataZoomComponent, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

const open = defineModel<boolean>({ required: true });
const props = defineProps<{ resource?: Resource }>();
type Preset = "30m" | "1h" | "custom";
const preset = ref<Preset>("1h");
const customRange = ref<[Date, Date] | null>(null);
const samples = ref<ResourceMetricSample[]>([]);
const loading = ref(false);
const paused = ref(false);
const error = ref("");
const chartElement = ref<HTMLElement>();
const selectedUnit = ref<"KB" | "%">("KB");
const memoryDisplayMode = ref<"absolute" | "relative">("absolute");
const axisWindow = ref<{ start?: number; end?: number; selected?: Record<string, boolean> }>({});
let chart: echarts.ECharts | undefined;
let refreshTimer: number | undefined;
let generation = 0;
let observer: ResizeObserver | undefined;

const cpu = computed(() => metricSeries(samples.value, "%"));
const memory = computed(() => metricSeries(samples.value, "KB"));
const relativeMemory = computed(() => relativeMetricSeries(memory.value));
const summaries = computed(() => metricSummaries(selectedUnit.value === "KB" ? memory.value : cpu.value));
const issueCount = computed(() => metricIssueCount(samples.value));
const latestAt = computed(() => samples.value.map(item => item.sampledAt).sort().at(-1));
const hasMetrics = computed(() => cpu.value.length > 0 || memory.value.length > 0);
const activeSeries = computed(() => selectedUnit.value === "KB"
  ? (memoryDisplayMode.value === "relative" ? relativeMemory.value.series : memory.value) : cpu.value);
const activeChartUnit = computed<"KB" | "%">(() => selectedUnit.value === "KB" && memoryDisplayMode.value === "relative" ? "%" : selectedUnit.value);
const activeDisplayUnit = computed(() => activeChartUnit.value === "KB" ? memoryUnit(activeSeries.value, axisWindow.value).unit : "%");
const zeroBaselineNames = computed(() => selectedUnit.value === "KB" && memoryDisplayMode.value === "relative"
  ? relativeMemory.value.zeroBaselineNames : []);

/** 当前选择范围始终转换为 ISO UTC，默认最近一小时，服务端再执行最终范围校验。 */
function selectedRange(): { start: Date; end: Date } {
  const end = new Date();
  if (preset.value === "custom" && customRange.value) return { start: customRange.value[0], end: customRange.value[1] };
  const minutes = preset.value === "30m" ? 30 : 60;
  return { start: new Date(end.getTime() - minutes * 60_000), end };
}
function rangeValid() {
  const { start, end } = selectedRange();
  return validMetricRange(start, end);
}
function number(value: number | null, unit: string) {
  return value === null ? "-" : `${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${unit}`;
}
function delta(value: number | null, unit: string) {
  if (value === null) return "无前次采样";
  return `${value > 0 ? "+" : ""}${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${unit}`;
}
function chartOption(series: ReturnType<typeof metricSeries>, unit: "KB" | "%"): echarts.EChartsCoreOption {
  const bounds = metricAxis(series, axisWindow.value);
  const display = unit === "KB" ? memoryUnit(series, axisWindow.value) : undefined;
  const precision = display ? memoryPrecision(bounds, display.factor) : 1;
  return {
    animation: false,
    grid: { left: 58, right: 20, top: 42, bottom: 52 },
    tooltip: { trigger: "axis", valueFormatter: (value: string | number) => unit === "KB" ? `${(Number(value) / (display?.factor ?? 1)).toLocaleString("zh-CN", { maximumFractionDigits: 6 })} ${display?.unit}` : `${value} %` },
    legend: { type: "scroll", top: 4, selected: axisWindow.value.selected },
    xAxis: { type: "time", splitNumber: 5, minInterval: 60_000, axisLabel: { hideOverlap: true, formatter: (value: number) => new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }) } },
    yAxis: { type: "value", name: unit === "KB" ? display?.unit : "%", min: bounds.min, max: bounds.max, splitNumber: 5, axisLabel: { hideOverlap: true, showMinLabel: false, showMaxLabel: false, formatter: (value: number) => unit === "KB" ? `${(value / (display?.factor ?? 1)).toLocaleString("zh-CN", { maximumFractionDigits: precision })} ${display?.unit}` : `${value.toLocaleString("zh-CN", { maximumFractionDigits: 1 })} %` }, scale: true },
    dataZoom: [{ type: "inside", ...zoomRange() }, { type: "slider", height: 16, bottom: 8, ...zoomRange() }],
    series: series.map(item => ({ name: item.name, type: "line", showSymbol: false, connectNulls: false, data: item.points, emphasis: { focus: "series" } })),
  };
}
/** 重新渲染时保留已选择的时间窗口，禁止纵轴更新反向重置横轴缩放。 */
function zoomRange() {
  return axisWindow.value.start === undefined || axisWindow.value.end === undefined
    ? { start: 0, end: 100 } : { startValue: axisWindow.value.start, endValue: axisWindow.value.end };
}
function updateAxis() {
  const option = chartOption(activeSeries.value, activeChartUnit.value);
  chart?.setOption({ yAxis: option.yAxis, tooltip: option.tooltip });
}
function renderCharts() {
  if (!chartElement.value) return;
  chart ??= echarts.init(chartElement.value);
  chart.setOption(chartOption(activeSeries.value, activeChartUnit.value), true);
  chart.off("legendselectchanged"); chart.off("datazoom");
  chart.on("legendselectchanged", (event: unknown) => { const selected = (event as { selected?: Record<string, boolean> }).selected; axisWindow.value = { ...axisWindow.value, selected }; updateAxis(); });
  chart.on("datazoom", (event: unknown) => {
    const change = ((event as { batch?: Array<{ start?: number; end?: number; startValue?: number; endValue?: number }>; start?: number; end?: number; startValue?: number; endValue?: number }).batch?.[0]) ?? event as { start?: number; end?: number; startValue?: number; endValue?: number };
    let low = Infinity, high = -Infinity;
    for (const item of activeSeries.value) for (const [time] of item.points) { low = Math.min(low, time); high = Math.max(high, time); }
    if (!Number.isFinite(low) || !Number.isFinite(high)) return;
    axisWindow.value = { ...axisWindow.value, start: change.startValue ?? (change.start === undefined ? undefined : low + (high - low) * change.start / 100), end: change.endValue ?? (change.end === undefined ? undefined : low + (high - low) * change.end / 100) };
    updateAxis();
  });
}
/** 图表节点由空态切换后才会挂载，渲染后再观察以保证窄屏和抽屉变化能触发 resize。 */
function observeCharts() {
  observer ??= new ResizeObserver(() => chart?.resize());
  if (chartElement.value) observer.observe(chartElement.value);
}
function disposeCharts() {
  chart?.dispose(); chart = undefined;
}
function scheduleRefresh() {
  if (refreshTimer) window.clearInterval(refreshTimer);
  refreshTimer = undefined;
  if (open.value && !paused.value) refreshTimer = window.setInterval(() => void load(), 60_000);
}
/** 使用请求代次隔离切换范围、关闭弹窗和轮询时迟到的历史分页响应。 */
async function load() {
  if (!props.resource || loading.value || !rangeValid()) return;
  const current = ++generation;
  const { start, end } = selectedRange();
  loading.value = true; error.value = "";
  const items: ResourceMetricSample[] = [];
  try {
    let cursor: string | undefined;
    let pages = 0;
    do {
      const page = await api.resourceMetrics(props.resource.id, { start: start.toISOString(), end: end.toISOString(), limit: 2000, cursor });
      if (current !== generation || !open.value) return;
      items.push(...page.items);
      cursor = page.nextCursor ?? undefined;
      pages += 1;
      if (pages >= 100) throw new Error("监控历史分页超过安全上限");
    } while (cursor);
    samples.value = items;
    await nextTick();
    if (current === generation && open.value) { renderCharts(); observeCharts(); }
  } catch (cause) {
    if (current === generation && open.value) {
      if (items.length) { samples.value = items; await nextTick(); renderCharts(); observeCharts(); }
      error.value = cause instanceof Error ? cause.message : "读取资源监控历史失败";
    }
  } finally {
    if (current === generation) loading.value = false;
  }
}
function reload() { generation += 1; loading.value = false; void load(); }
function choosePreset(value: Preset) { preset.value = value; if (value !== "custom") reload(); }
function applyCustomRange() {
  if (!rangeValid()) { ElMessage.warning("自定义时间范围必须在当前时间前且不超过 31 天"); return; }
  reload();
}
function togglePause() { paused.value = !paused.value; scheduleRefresh(); if (!paused.value) void load(); }
function selectMetric(unit: "KB" | "%") { selectedUnit.value = unit; axisWindow.value = {}; void nextTick(renderCharts); }
/** 切换仅改变内存图展示方式；相对变化率的基准来自当前已查询的完整样本。 */
function selectMemoryDisplay(mode: "absolute" | "relative") { memoryDisplayMode.value = mode; axisWindow.value = {}; void nextTick(renderCharts); }
function exportCsv() {
  const blob = new Blob([metricsCsv(samples.value)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = `${props.resource?.name ?? "resource"}-resource-metrics.csv`; anchor.click();
  URL.revokeObjectURL(url);
}
watch(() => [open.value, props.resource?.id] as const, ([visible]) => {
  generation += 1;
  if (visible) {
    paused.value = false;
    memoryDisplayMode.value = "absolute";
    void nextTick(observeCharts);
    loading.value = false; reload();
  } else {
    loading.value = false; samples.value = []; error.value = ""; disposeCharts(); observer?.disconnect(); observer = undefined;
  }
  scheduleRefresh();
}, { immediate: true });
watch([cpu, memory], () => { if (open.value) void nextTick(() => { renderCharts(); observeCharts(); }); });
onBeforeUnmount(() => { generation += 1; if (refreshTimer) window.clearInterval(refreshTimer); observer?.disconnect(); disposeCharts(); });
</script>
<template>
  <el-dialog v-model="open" :title="`${props.resource?.name ?? '资源'} · CPU 与内存趋势`" width="min(1100px, 96vw)" destroy-on-close class="resource-metrics-dialog">
    <div class="metrics-toolbar">
      <el-radio-group :model-value="selectedUnit" aria-label="监控指标" @change="selectMetric"><el-radio-button value="KB">内存</el-radio-button><el-radio-button value="%">CPU</el-radio-button></el-radio-group>
      <el-tooltip v-if="selectedUnit === 'KB'" content="比率以当前查询范围内每条曲线的首个有效采样为基准；缩放图表不会改变基准，刷新或重新查询后会按新范围计算。" :popper-style="{ maxWidth: 'min(320px, calc(100vw - 24px))', whiteSpace: 'normal' }">
        <el-radio-group :model-value="memoryDisplayMode" aria-label="内存图展示方式" @change="selectMemoryDisplay"><el-radio-button value="absolute">原始数值</el-radio-button><el-radio-button value="relative">比率</el-radio-button></el-radio-group>
      </el-tooltip>
      <el-radio-group :model-value="preset" aria-label="监控历史时间范围" @change="choosePreset">
        <el-radio-button value="30m">最近 30 分钟</el-radio-button><el-radio-button value="1h">最近 1 小时</el-radio-button><el-radio-button value="custom">自定义</el-radio-button>
      </el-radio-group>
      <div class="metrics-commands">
        <el-tooltip content="刷新趋势"><el-button circle :icon="RefreshCw" aria-label="刷新趋势" :loading="loading" @click="load" /></el-tooltip>
        <el-tooltip :content="paused ? '恢复自动刷新' : '暂停自动刷新'"><el-button circle :icon="paused ? Play : Pause" :aria-label="paused ? '恢复自动刷新' : '暂停自动刷新'" @click="togglePause" /></el-tooltip>
        <el-tooltip content="导出原始采样值，不受图表比率展示影响"><el-button :icon="Download" :disabled="!samples.length" @click="exportCsv">导出 CSV</el-button></el-tooltip>
      </div>
    </div>
    <div v-if="preset === 'custom'" class="metrics-custom-range"><el-date-picker v-model="customRange" type="datetimerange" range-separator="至" start-placeholder="开始时间" end-placeholder="结束时间" :teleported="false" /><el-button type="primary" @click="applyCustomRange">查询</el-button></div>
    <el-alert v-if="error" type="error" show-icon :title="error" :closable="false" class="metrics-alert" />
    <el-alert v-if="zeroBaselineNames.length" type="warning" show-icon :closable="false" class="metrics-alert" :title="`以下曲线的首个有效采样为 0，无法计算变化率：${zeroBaselineNames.join('、')}`" />
    <div class="metrics-summary" aria-label="资源监控摘要">
      <div v-for="item in summaries" :key="item.key"><span>{{ item.name }} 最新值</span><strong>{{ number(item.value, item.unit) }}</strong><small>{{ delta(item.delta, item.unit) }}</small></div>
      <div><span>采样状态</span><strong>{{ samples.length }} 条</strong><small>{{ issueCount ? `${issueCount} 条异常` : '无异常样本' }}</small></div>
      <div><span>最后采样</span><strong>{{ latestAt ? new Date(latestAt).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '-' }}</strong><small>{{ paused ? '自动刷新已暂停' : '每分钟刷新' }}</small></div>
    </div>
    <div v-loading="loading" class="metrics-content">
      <el-empty v-if="!loading && !error && !hasMetrics" description="所选时间范围内暂无资源监控数据" />
      <template v-else>
        <section class="metrics-chart-section"><h3>{{ selectedUnit === 'KB' ? (memoryDisplayMode === 'relative' ? '内存变化率（%）' : `内存（${activeDisplayUnit}）`) : 'CPU（%）' }}</h3><div ref="chartElement" class="metrics-chart" :aria-label="selectedUnit === 'KB' ? (memoryDisplayMode === 'relative' ? '内存变化率图' : '内存趋势图') : 'CPU 趋势图'" /></section>
      </template>
    </div>
  </el-dialog>
</template>
<style scoped>
.metrics-toolbar, .metrics-commands, .metrics-custom-range { display: flex; gap: 12px; align-items: center; }
.metrics-toolbar { justify-content: space-between; flex-wrap: wrap; }
.metrics-custom-range { margin-top: 12px; flex-wrap: wrap; }
.metrics-alert { margin-top: 12px; }
.metrics-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 16px 0; }
.metrics-summary > div { border: 1px solid var(--el-border-color-lighter); border-radius: 6px; padding: 12px; min-width: 0; }
.metrics-summary span, .metrics-summary small { display: block; color: var(--el-text-color-secondary); font-size: 12px; overflow-wrap: anywhere; }
.metrics-summary strong { display: block; margin: 6px 0; font-size: 16px; overflow-wrap: anywhere; }
.metrics-chart-section + .metrics-chart-section { margin-top: 20px; }
.metrics-chart-section h3 { margin: 0 0 8px; font-size: 15px; font-weight: 600; }
.metrics-chart { height: 340px; width: 100%; }
@media (max-width: 767px) { .metrics-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } .metrics-chart { height: 220px; } .metrics-commands { width: 100%; } }
</style>
