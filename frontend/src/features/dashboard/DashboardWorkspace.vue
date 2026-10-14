<script setup lang="ts">
// 默认首页只呈现后端授权后的汇总；刷新失败保留最近成功快照，避免瞬态网络错误清空运营判断。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts/core";
import { LineChart, PieChart } from "echarts/charts";
import { GridComponent, TitleComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { CalendarDays, Database, Pause, Play, RefreshCw, Server, Waypoints } from "lucide-vue-next";
import { api } from "../../shared/api";
import type { DashboardSnapshot } from "../../shared/types";
import { formatDashboardBytes, nodeHealthLabel, shanghaiDate, trendPoints } from "./dashboard";

echarts.use([LineChart, PieChart, GridComponent, TitleComponent, TooltipComponent, CanvasRenderer]);

withDefaults(defineProps<{ canOpenResources?: boolean; canOpenTasks?: boolean; canOpenNodes?: boolean }>(), {
  canOpenResources: false, canOpenTasks: false, canOpenNodes: false,
});
const emit = defineEmits<{ openResources: []; openTasks: []; openNodes: [] }>();
const days = ref<7 | 14 | 30>(7);
const date = ref(shanghaiDate());
const snapshot = ref<DashboardSnapshot>();
const loading = ref(false);
const stale = ref(false);
const error = ref("");
const paused = ref(false);
const chartElement = ref<HTMLElement>();
const resourceHealthElement = ref<HTMLElement>();
const lastSuccessAt = ref("");
let chart: echarts.ECharts | undefined;
let resourceHealthChart: echarts.ECharts | undefined;
let chartObserver: ResizeObserver | undefined;
let refreshTimer: number | undefined;
let requestGeneration = 0;
let disposed = false;

const hasData = computed(() => Boolean(snapshot.value));
const summary = computed(() => snapshot.value?.overview);
const topResources = computed(() => snapshot.value?.topResources ?? []);
const nodes = computed(() => snapshot.value?.nodes ?? []);
const latestDaily = computed(() => snapshot.value?.daily?.at(-1));
const dateHint = computed(() => snapshot.value ? `${snapshot.value.range.startDate} 至 ${snapshot.value.range.endDate}` : "等待汇总数据");
const logAvailable = computed(() => Boolean(snapshot.value?.availability.logs));
const nodesAvailable = computed(() => Boolean(snapshot.value?.availability.nodes));
const coredumps = computed(() => snapshot.value?.coredumps ?? null);
const refreshStatus = computed(() => paused.value ? "自动刷新已暂停" : "每 30 秒自动刷新");
const maxTopBytes = computed(() => Math.max(1, ...topResources.value.map(item => item.bytes)));
const lastUpdated = computed(() => lastSuccessAt.value ? new Date(lastSuccessAt.value).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "尚未成功更新");

function renderChart() {
  if (!chartElement.value || !snapshot.value) return;
  if (chart && chart.getDom() !== chartElement.value) { chart.dispose(); chart = undefined; }
  chart ??= echarts.init(chartElement.value);
  const points = trendPoints(snapshot.value.daily ?? []);
  chart.setOption({
    animationDuration: 260,
    grid: { top: 18, right: 14, bottom: 28, left: 58, containLabel: true },
    tooltip: {
      trigger: "axis", className: "dashboard-chart-tooltip",
      formatter: (params: unknown) => {
        const item = (params as Array<{ axisValueLabel: string; value: number | null }>)[0];
        return `${item?.axisValueLabel ?? ""}<br/>日志量　<strong>${item?.value == null ? "暂无数据" : formatDashboardBytes(item.value)}</strong>`;
      },
    },
    xAxis: { type: "category", data: points.map(item => item.date.slice(5)), boundaryGap: false, axisTick: { show: false }, axisLine: { lineStyle: { color: "#cbd8d8" } }, axisLabel: { color: "#71858a", fontSize: 11 } },
    yAxis: { type: "value", axisLabel: { color: "#71858a", formatter: (value: number) => formatDashboardBytes(value) }, splitLine: { lineStyle: { color: "#e7eeee" } } },
    series: [{ type: "line", data: points.map(item => item.bytes), smooth: true, connectNulls: false, showSymbol: false, lineStyle: { color: "#168a7a", width: 2 }, areaStyle: { color: "rgba(22, 138, 122, .12)" } }],
  }, { notMerge: true });
}
function renderResourceHealth() {
  if (!resourceHealthElement.value || !summary.value) return;
  if (resourceHealthChart && resourceHealthChart.getDom() !== resourceHealthElement.value) { resourceHealthChart.dispose(); resourceHealthChart = undefined; }
  resourceHealthChart ??= echarts.init(resourceHealthElement.value);
  const online = Math.max(0, summary.value.onlineResources);
  const offline = Math.max(0, summary.value.resources - online);
  resourceHealthChart.setOption({
    animationDuration: 260,
    title: { text: `${summary.value.resources ? Math.round(online / summary.value.resources * 100) : 0}%`, subtext: "在线设备", left: "center", top: "28%", textStyle: { color: "#263839", fontSize: 17, fontWeight: 600 }, subtextStyle: { color: "#839195", fontSize: 10 } },
    tooltip: { trigger: "item", formatter: (item: { name: string; value: number }) => `${item.name}　${item.value} 台` },
    series: [{ type: "pie", radius: ["61%", "78%"], center: ["50%", "52%"], label: { show: false }, data: [{ name: "在线", value: online, itemStyle: { color: "#41c9c5" } }, { name: "其他状态", value: offline, itemStyle: { color: "#31586c" } }] }],
  }, { notMerge: true });
}
function observeChart() {
  chartObserver?.disconnect();
  chartObserver = new ResizeObserver(() => { chart?.resize(); resourceHealthChart?.resize(); });
  if (chartElement.value) chartObserver.observe(chartElement.value);
  if (resourceHealthElement.value) chartObserver.observe(resourceHealthElement.value);
}
async function load(quiet = false) {
  if (quiet && loading.value) return;
  const current = ++requestGeneration;
  loading.value = true;
  if (!quiet) error.value = "";
  try {
    const value = await api.dashboard(days.value, date.value);
    if (disposed || current !== requestGeneration) return;
    snapshot.value = value;
    lastSuccessAt.value = value.generatedAt;
    stale.value = false;
    error.value = "";
    await nextTick();
    renderChart(); renderResourceHealth(); observeChart();
  } catch (cause) {
    if (disposed || current !== requestGeneration) return;
    stale.value = Boolean(snapshot.value);
    error.value = cause instanceof Error ? cause.message : "资源看板加载失败";
  } finally {
    if (!disposed && current === requestGeneration) loading.value = false;
  }
}
function scheduleRefresh() {
  if (refreshTimer) window.clearInterval(refreshTimer);
  refreshTimer = paused.value ? undefined : window.setInterval(() => void load(true), 30_000);
}
function selectDays(value: 7 | 14 | 30) { days.value = value; }
function togglePause() { paused.value = !paused.value; scheduleRefresh(); if (!paused.value) void load(true); }
function statusType(status: string) { return status === "HEALTHY" ? "success" : status === "WARNING" ? "warning" : status === "CRITICAL" || status === "OFFLINE" ? "danger" : "info"; }
function receivedAt(value: string | null) { return value ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "暂无记录"; }
function disableFutureDate(value: Date) { return shanghaiDate(value) > shanghaiDate(); }
function rankRatio(bytes: number) { return `${Math.max(3, Math.round(bytes / maxTopBytes.value * 100))}%`; }

watch([days, date], () => void load());
onMounted(() => { void load(); scheduleRefresh(); });
onBeforeUnmount(() => { disposed = true; requestGeneration += 1; if (refreshTimer) window.clearInterval(refreshTimer); chartObserver?.disconnect(); chart?.dispose(); resourceHealthChart?.dispose(); });
defineExpose({ reload: () => load() });
</script>

<template>
  <section class="dashboard" aria-label="资源看板">
    <header class="dashboard-head">
      <div><p class="dashboard-eyebrow">RESOURCE OVERVIEW</p><h1>资源看板</h1><span>{{ dateHint }}</span></div>
      <div class="dashboard-controls">
        <el-radio-group :model-value="days" aria-label="统计时间范围" size="small" @change="selectDays"><el-radio-button :value="7">7 天</el-radio-button><el-radio-button :value="14">14 天</el-radio-button><el-radio-button :value="30">30 天</el-radio-button></el-radio-group>
        <el-date-picker v-model="date" aria-label="选定统计日期" type="date" value-format="YYYY-MM-DD" :clearable="false" :editable="false" :disabled-date="disableFutureDate" />
        <el-tooltip content="手动刷新"><el-button circle :icon="RefreshCw" :loading="loading" aria-label="刷新资源看板" @click="() => load()" /></el-tooltip>
        <el-tooltip :content="paused ? '恢复每 30 秒自动刷新' : '暂停自动刷新'"><el-button circle :icon="paused ? Play : Pause" :aria-label="paused ? '恢复自动刷新' : '暂停自动刷新'" @click="togglePause" /></el-tooltip>
      </div>
    </header>

    <el-alert v-if="error" :title="stale ? `数据可能过期：${error}` : error" :type="stale ? 'warning' : 'error'" :closable="false" show-icon class="dashboard-alert" />
    <div v-loading="loading && !hasData" class="dashboard-body">
      <el-empty v-if="!loading && !hasData && !error" description="所选日期暂无运行汇总数据" />
      <template v-else-if="hasData && summary">
        <section class="dashboard-overview" aria-label="资源概览">
          <button class="overview-card overview-card-accent" :disabled="!canOpenResources" @click="emit('openResources')"><span><Database :size="17" />设备资源</span><strong>{{ summary.resources }}</strong><small>{{ summary.onlineResources }} 台在线</small></button>
          <button class="overview-card" :disabled="!canOpenTasks" @click="emit('openTasks')"><span><Waypoints :size="17" />活跃采集</span><strong>{{ summary.collectingTasks }}</strong><small>共 {{ summary.tasks }} 个任务 · 其他状态 {{ Math.max(0, summary.tasks - summary.collectingTasks) }}</small></button>
          <article class="overview-card"><span><CalendarDays :size="17" />当日日志量</span><strong>{{ logAvailable ? formatDashboardBytes(latestDaily?.bytes) : '无权限' }}</strong><small>{{ logAvailable ? `${latestDaily?.fileCount ?? 0} 个日志分卷` : '当前账号无日志统计权限' }}</small></article>
          <button class="overview-card" :disabled="!canOpenNodes || !nodesAvailable" @click="emit('openNodes')"><span><Server :size="17" />Worker</span><strong>{{ nodesAvailable ? summary.nodes : '无权限' }}</strong><small>{{ nodesAvailable ? (nodes.length ? `${nodes.filter(item => item.healthLevel === 'HEALTHY').length} 个健康` : '尚未收到健康状态') : '节点详情受权限控制' }}</small></button>
          <article class="overview-card overview-chart-card"><span>设备连通性</span><div ref="resourceHealthElement" class="resource-health-chart" aria-label="资源在线占比图" /></article>
        </section>

        <section class="dashboard-main-grid">
          <article class="dashboard-panel dashboard-trend"><header><div><p>日志趋势</p><h2>每日采集量</h2></div><span>{{ days }} 日窗口</span></header><el-empty v-if="!logAvailable" :image-size="56" description="当前账号无日志统计权限" /><div v-else ref="chartElement" class="dashboard-chart" aria-label="每日日志量趋势图" /></article>
          <article class="dashboard-panel dashboard-rank"><header><div><p>设备排行</p><h2>选定日日志量 Top</h2></div><span>{{ date }}</span></header><el-empty v-if="!logAvailable" :image-size="56" description="当前账号无日志统计权限" /><el-empty v-else-if="!topResources.length" :image-size="56" description="该日暂无设备日志" /><template v-else><ol><li v-for="(item, index) in topResources" :key="item.resourceId"><span class="rank-number">{{ index + 1 }}</span><div><strong>{{ item.name }}</strong><small>{{ item.ip }} · {{ item.fileCount }} 个分卷</small><i class="rank-bar"><i :style="{ width: rankRatio(item.bytes) }" /></i></div><b>{{ formatDashboardBytes(item.bytes) }}</b></li></ol><p v-if="snapshot?.topResourcesTruncated" class="dashboard-panel-note">仅展示前 20 台设备，统计总量未截断。</p></template></article>
        </section>

        <section class="dashboard-detail-grid">
          <article class="dashboard-panel dashboard-core"><header><div><p>Core 文件</p><h2>选定日资源明细</h2></div><el-button v-if="canOpenResources" text size="small" @click="emit('openResources')">查看设备</el-button></header><el-empty v-if="!coredumps" :image-size="42" description="当前账号无 Core 统计权限" /><template v-else><dl><div><dt>涉及设备</dt><dd>{{ coredumps.resourceCount }}</dd></div><div><dt>文件数量</dt><dd>{{ coredumps.fileCount }}</dd></div><div><dt>占用容量</dt><dd>{{ formatDashboardBytes(coredumps.bytes) }}</dd></div></dl><el-empty v-if="!coredumps.items.length" :image-size="42" description="该日暂无可见 Core 文件" /><template v-else><ul class="core-items"><li v-for="item in coredumps.items" :key="item.resourceId"><div><strong>{{ item.name }}</strong><small>{{ item.ip }} · 最近 {{ receivedAt(item.lastReceivedAt) }}</small></div><b>{{ item.fileCount }} 个 · {{ formatDashboardBytes(item.bytes) }}</b></li></ul><p v-if="coredumps.truncated" class="dashboard-panel-note">仅展示前 20 台设备，统计总量未截断。</p></template></template></article>
          <article class="dashboard-panel dashboard-workers"><header><div><p>Worker 健康</p><h2>服务状态</h2></div><el-button v-if="canOpenNodes && nodesAvailable" text size="small" @click="emit('openNodes')">查看节点</el-button></header><el-empty v-if="!nodesAvailable" :image-size="46" description="当前账号无节点健康详情权限" /><el-empty v-else-if="!nodes.length" :image-size="46" description="尚未收到 Worker 健康状态" /><ul v-else><li v-for="item in nodes" :key="item.id"><div><strong>{{ item.name }}</strong><small>{{ item.accepting ? `准入中 · ${item.activeTasks}/${item.capacity} 个任务` : `暂不准入 · ${item.activeTasks}/${item.capacity} 个任务` }}</small></div><el-tooltip :content="item.reasons.length ? item.reasons.join('；') : '服务端未报告异常原因'" placement="top"><el-tag :type="statusType(item.healthLevel)" effect="plain" size="small">{{ nodeHealthLabel(item.healthLevel) }}</el-tag></el-tooltip></li></ul></article>
        </section>
      </template>
    </div>
    <footer class="dashboard-status"><span>最近成功更新：{{ lastUpdated }}</span><span>{{ refreshStatus }}</span></footer>
    <p v-if="snapshot?.notes?.length" class="dashboard-notes">{{ snapshot.notes.join(' ') }}</p>
  </section>
</template>

<style scoped>
.dashboard { padding: 34px 0 36px; color: #354449; }
.dashboard-head, .dashboard-controls, .dashboard-panel header, .dashboard-panel li { display: flex; align-items: center; }
.dashboard-head { justify-content: space-between; gap: 20px; padding-bottom: 24px; }
.dashboard-eyebrow { color: #087b70; font: 600 10px ui-monospace, monospace; letter-spacing: 1.2px; margin-bottom: 7px; }.dashboard h1 { color: #253536; font-size: 28px; }.dashboard-head > div > span { color: #829094; font-size: 12px; }
.dashboard-controls { justify-content: flex-end; flex-wrap: wrap; gap: 8px; }.dashboard-controls :deep(.el-radio-button__inner) { color: #637378; }.dashboard-controls :deep(.el-radio-button__original-radio:checked + .el-radio-button__inner) { background: #087b70; border-color: #087b70; color: #fff; box-shadow: -1px 0 0 0 #087b70; }
.dashboard-alert { margin-bottom: 16px; }.dashboard-body { min-height: 340px; }.dashboard-overview { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }.overview-card { min-height: 126px; padding: 18px; border: 1px solid #dce6e6; border-radius: 6px; color: #354449; text-align: left; background: #fff; }.overview-card:is(button) { cursor: pointer; }.overview-card:disabled { cursor: default; }.overview-card:not(:disabled):hover, .overview-card:not(:disabled):focus-visible { border-color: #53a99e; outline: none; }.overview-card-accent { border-top: 3px solid #168a7a; }.overview-card span { display: flex; align-items: center; gap: 7px; color: #718186; font-size: 12px; }.overview-card strong { display: block; margin: 15px 0 4px; color: #263839; font-size: 26px; line-height: 1; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }.overview-card small { color: #839195; font-size: 11px; }.overview-chart-card { padding: 14px 18px 6px; }.resource-health-chart { height: 92px; width: 100%; }
.dashboard-main-grid, .dashboard-detail-grid { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(300px, .9fr); gap: 12px; margin-bottom: 12px; }.dashboard-panel { min-width: 0; border: 1px solid #dce6e6; border-radius: 6px; background: #fff; }.dashboard-panel header { justify-content: space-between; min-height: 62px; padding: 14px 16px; border-bottom: 1px solid #e5ecec; }.dashboard-panel header p { color: #087b70; font-size: 11px; }.dashboard-panel h2 { margin: 4px 0 0; color: #2b3b3d; font-size: 14px; }.dashboard-panel header > span { color: #829094; font-size: 11px; }.dashboard-panel header :deep(.el-button) { color: #087b70; }.dashboard-chart, .resource-health-chart { min-width: 0; }.dashboard-chart { height: 270px; width: 100%; }.dashboard-rank ol, .dashboard-workers ul, .core-items { margin: 0; padding: 0; list-style: none; }.dashboard-panel li { gap: 10px; min-height: 54px; padding: 9px 16px; border-bottom: 1px solid #edf1f1; }.dashboard-panel li:last-child { border-bottom: 0; }.rank-number { width: 18px; color: #087b70; font: 600 12px ui-monospace, monospace; }.dashboard-panel li > div { flex: 1; min-width: 0; }.dashboard-panel li strong, .dashboard-panel li small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.dashboard-panel li strong { color: #354749; font-size: 12px; }.dashboard-panel li small { margin-top: 3px; color: #7d8b8f; font-size: 10px; }.dashboard-rank b, .core-items b { color: #27665e; font-size: 12px; font-weight: 600; white-space: nowrap; }.rank-bar { display: block; height: 3px; margin-top: 6px; overflow: hidden; border-radius: 2px; background: #e3eeee; }.rank-bar > i { display: block; height: 100%; border-radius: inherit; background: #35a99d; }.dashboard-core dl { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin: 0; }.dashboard-core dl > div { padding: 18px 16px; border-right: 1px solid #e5ecec; }.dashboard-core dl > div:last-child { border-right: 0; }.dashboard-core dt { color: #7f8f93; font-size: 11px; }.dashboard-core dd { margin: 8px 0 0; color: #2d4244; font-size: 18px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }.core-items { border-top: 1px solid #e5ecec; }.dashboard-panel-note { margin: 0; padding: 10px 16px; border-top: 1px solid #edf1f1; color: #829094; font-size: 10px; }.dashboard-workers :deep(.el-tag) { flex: 0 0 auto; }.dashboard-status, .dashboard-notes { color: #809095; font-size: 11px; }.dashboard-status { display: flex; justify-content: space-between; gap: 12px; margin-top: 16px; }.dashboard-notes { margin-top: 8px; line-height: 1.55; }
@media (max-width: 1100px) { .dashboard-overview { grid-template-columns: repeat(3, minmax(0, 1fr)); } }.dashboard-status { flex-wrap: wrap; }
@media (max-width: 900px) { .dashboard-head { align-items: flex-start; flex-direction: column; }.dashboard-head > div, .dashboard-controls { min-width: 0; }.dashboard-controls { width: 100%; justify-content: flex-start; }.dashboard-overview { grid-template-columns: repeat(2, minmax(0, 1fr)); }.dashboard-main-grid, .dashboard-detail-grid { grid-template-columns: 1fr; } }
@media (max-width: 700px) { .dashboard { padding: 24px 0 28px; }.dashboard-head { align-items: flex-start; flex-direction: column; }.dashboard-controls { width: 100%; justify-content: flex-start; }.dashboard-controls :deep(.el-date-editor) { width: 158px; }.dashboard-overview { gap: 8px; }.overview-card { min-height: 112px; padding: 14px; }.overview-card strong { font-size: 22px; }.overview-chart-card { padding: 12px 14px 4px; }.dashboard-chart { height: 220px; }.dashboard-core dl { grid-template-columns: 1fr; }.dashboard-core dl > div { border-right: 0; border-bottom: 1px solid #e5ecec; }.dashboard-core dl > div:last-child { border-bottom: 0; } }
</style>
