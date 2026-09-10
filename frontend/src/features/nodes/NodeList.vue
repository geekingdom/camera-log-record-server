<script setup lang="ts">
// 节点看板只呈现服务端给出的事实与新鲜遥测，不推断趋势或调度评分。
import { computed, onMounted, onUnmounted, ref } from "vue";
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  CircleOff,
  Cpu,
  HardDrive,
  HeartPulse,
  MemoryStick,
  Server,
  ShieldCheck,
  Timer,
  WifiOff,
} from "lucide-vue-next";
import type { Node } from "../../shared/types";
import {
  currentHealth,
  currentTelemetry,
  formatBytes,
  formatLatency,
  formatRate,
  nodeAbnormal,
  nodeAdmissible,
  nodeOnline,
  percentage,
} from "./nodeDashboard";

const props = defineProps<{ items: Node[]; loading: boolean }>();
const now = ref(Date.now());
let clock: ReturnType<typeof setInterval> | undefined;
onMounted(() => { clock = setInterval(() => { now.value = Date.now(); }, 1000); });
onUnmounted(() => clearInterval(clock));
const orderedNodes = computed(() => [...props.items].sort((a, b) => a.id.localeCompare(b.id)));
const onlineCount = computed(() => props.items.filter(node => nodeOnline(node, now.value)).length);
const admissibleCount = computed(() => props.items.filter(node => nodeAdmissible(node, now.value)).length);
const abnormalCount = computed(() => props.items.filter(node => nodeAbnormal(node, now.value)).length);

function address(node: Node) { return node.url || node.address || "未上报服务地址"; }
function healthLabel(node: Node) {
  if (!nodeOnline(node, now.value)) return "离线";
  const status = currentHealth(node)?.status;
  return ({ HEALTHY: "健康", WARNING: "需关注", CRITICAL: "严重", UNKNOWN: "未知", OFFLINE: "离线" }[status ?? ""] ?? "暂无健康数据");
}
function healthTone(node: Node) {
  if (!nodeOnline(node, now.value)) return "danger";
  const status = currentHealth(node)?.status;
  return ({ HEALTHY: "success", WARNING: "warning", CRITICAL: "danger", OFFLINE: "danger", UNKNOWN: "info" }[status ?? ""] ?? "info");
}
function onlineLabel(node: Node) { return nodeOnline(node, now.value) ? "在线" : "失联"; }
function admissionLabel(node: Node) {
  if (!nodeOnline(node, now.value)) return "节点失联";
  if (node.isolated) return "节点已隔离";
  if (node.configurationMismatch) return "地址配置不一致";
  if (!node.accepting) return "暂停接入";
  if (currentHealth(node)?.status === "CRITICAL") return "健康严重";
  if (Number(node.diskPercent ?? 0) >= 90) return "磁盘压力过高";
  if (Number(node.writeLatencyMs ?? 0) > 200) return "写入延迟过高";
  if (Number(currentTelemetry(node, now.value)?.cpuPercent ?? 0) >= 95) return "CPU 压力过高";
  if (Number(currentTelemetry(node, now.value)?.memoryPercent ?? 0) >= 95) return "内存压力过高";
  if (Number(node.activeTasks ?? 0) >= Number(node.capacity ?? 0)) return "容量已满";
  return "允许接入";
}
function metricValue(value?: number | null) { return percentage(value) ?? 0; }
function metricText(value?: number | null) { return percentage(value) === undefined ? "暂无数据" : `${percentage(value)}%`; }
function telemetryScope(node: Node) {
  const scope = currentTelemetry(node, now.value)?.scope;
  return scope === "HOST" ? "主机采样" : scope === "RUNTIME" ? "运行时采样" : "暂无采样";
}
function sampledAt(node: Node) {
  const value = currentTelemetry(node, now.value)?.sampledAt;
  return value ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "暂无采样";
}
function diskText(node: Node) { return metricText(node.diskPercent); }
function gaugeTone(value: number | undefined | null, warning: number, critical: number) {
  if (value == null) return undefined;
  if (value >= critical) return "exception";
  if (value >= warning) return "warning";
  return undefined;
}
function memoryText(node: Node) {
  const telemetry = currentTelemetry(node, now.value);
  if (telemetry?.memoryUsedBytes === undefined || telemetry.memoryTotalBytes === undefined) return metricText(telemetry?.memoryPercent);
  return `${formatBytes(telemetry.memoryUsedBytes)} / ${formatBytes(telemetry.memoryTotalBytes)}`;
}
</script>

<template>
  <section class="node-dashboard" aria-label="服务节点运行看板">
    <div class="node-summary" aria-label="节点汇总">
      <div class="node-summary-item"><span><Server :size="16" />全部节点</span><strong>{{ items.length }}</strong></div>
      <div class="node-summary-item online"><span><Activity :size="16" />在线节点</span><strong>{{ onlineCount }}</strong><small>心跳有效</small></div>
      <div class="node-summary-item admissible"><span><ShieldCheck :size="16" />可准入</span><strong>{{ admissibleCount }}</strong><small>可接收新任务</small></div>
      <div class="node-summary-item abnormal"><span><AlertTriangle :size="16" />异常节点</span><strong>{{ abnormalCount }}</strong><small>失联或需关注</small></div>
    </div>

    <div v-loading="loading" class="node-card-list">
      <el-empty v-if="!loading && !items.length" description="暂无节点" />
      <article v-for="node in orderedNodes" :key="node.id" class="node-card" :class="{ offline: !nodeOnline(node, now), abnormal: nodeAbnormal(node, now) }">
        <header class="node-card-head">
          <div class="node-identity"><span class="node-icon"><Server :size="19" /></span><div><h2>{{ node.name || node.id }}</h2><code>{{ node.id }}</code></div></div>
          <div class="node-state-tags"><el-tag class="node-state-pill" :type="nodeOnline(node, now) ? 'success' : 'danger'" effect="plain"><CheckCircle2 v-if="nodeOnline(node, now)" :size="14" /><WifiOff v-else :size="14" />{{ onlineLabel(node) }}</el-tag><el-tag v-if="currentHealth(node)" class="node-state-pill" :type="healthTone(node)" effect="plain"><HeartPulse :size="14" />{{ healthLabel(node) }}</el-tag></div>
        </header>
        <p class="node-address">{{ address(node) }}</p>

        <div class="node-facts">
          <div><span>任务容量</span><strong>{{ node.activeTasks ?? "暂无数据" }}<template v-if="node.capacity !== undefined"> / {{ node.capacity }}</template></strong></div>
          <div><span>新任务准入</span><strong :class="{ admitted: nodeAdmissible(node, now), blocked: !nodeAdmissible(node, now) }">{{ admissionLabel(node) }}</strong></div>
          <div><span>写入延迟</span><strong>{{ formatLatency(node) }}</strong></div>
          <div><span>遥测范围</span><strong>{{ telemetryScope(node) }}</strong></div>
        </div>

        <div class="node-gauges" :class="{ unavailable: !currentTelemetry(node, now) }">
          <div class="node-gauge"><div class="gauge-label"><span><Cpu :size="15" />CPU</span><strong>{{ metricText(currentTelemetry(node, now)?.cpuPercent) }}</strong></div><el-progress :percentage="metricValue(currentTelemetry(node, now)?.cpuPercent)" :status="gaugeTone(currentTelemetry(node, now)?.cpuPercent, 80, 95)" :show-text="false" :stroke-width="8" /></div>
          <div class="node-gauge"><div class="gauge-label"><span><MemoryStick :size="15" />内存</span><strong>{{ memoryText(node) }}</strong></div><el-progress :percentage="metricValue(currentTelemetry(node, now)?.memoryPercent)" :status="gaugeTone(currentTelemetry(node, now)?.memoryPercent, 85, 95)" :show-text="false" :stroke-width="8" /></div>
          <div class="node-gauge"><div class="gauge-label"><span><HardDrive :size="15" />磁盘</span><strong>{{ diskText(node) }}</strong></div><el-progress :percentage="metricValue(node.diskPercent)" :status="gaugeTone(node.diskPercent, 80, 90)" :show-text="false" :stroke-width="8" /></div>
        </div>

        <div class="node-footer">
          <div class="node-rate"><ArrowUp :size="15" /><span>上行</span><strong>{{ formatRate(currentTelemetry(node, now)?.networkUploadBytesPerSecond) }}</strong></div>
          <div class="node-rate"><ArrowDown :size="15" /><span>下行</span><strong>{{ formatRate(currentTelemetry(node, now)?.networkDownloadBytesPerSecond) }}</strong></div>
          <div class="node-rate"><Activity :size="15" /><span>采集输入</span><strong>{{ formatRate(node.inputBytesPerSecond) }}</strong></div>
          <div class="node-sample"><Timer :size="14" />{{ sampledAt(node) }}</div>
        </div>
        <div v-if="currentHealth(node)?.reasons?.length" class="node-reasons"><CircleOff :size="15" /><span>{{ currentHealth(node)?.reasons?.join("；") }}</span></div>
        <div v-else-if="!currentTelemetry(node, now)" class="node-no-data">尚无新鲜遥测数据，未显示健康结论。</div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.node-dashboard { display: grid; gap: 20px; }
.node-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-top: 1px solid #dce5e4; border-bottom: 1px solid #dce5e4; background: #fff; }
.node-summary-item { display: grid; grid-template-columns: 1fr auto; gap: 5px 12px; min-width: 0; padding: 20px 24px; border-right: 1px solid #e7eeec; }
.node-summary-item:last-child { border-right: 0; }
.node-summary-item span { display: flex; align-items: center; gap: 7px; color: #708084; font-size: 12px; }
.node-summary-item strong { grid-row: span 2; align-self: center; color: #253436; font-size: 29px; font-variant-numeric: tabular-nums; }
.node-summary-item small { color: #9aa6a8; font-size: 11px; }
.node-summary-item.online strong, .node-summary-item.admissible strong { color: #168267; }
.node-summary-item.abnormal strong { color: #bd5a26; }
.node-card-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; min-height: 160px; }
.node-card { display: grid; gap: 15px; min-width: 0; padding: 20px; border: 1px solid #dfe7e5; border-radius: 6px; background: #fff; }
.node-card.abnormal { border-left: 3px solid #d67a32; padding-left: 18px; }
.node-card.offline { border-left-color: #c34d4d; }
.node-card-head, .node-identity, .node-state-tags, .node-footer, .node-rate, .node-sample { display: flex; align-items: center; }
.node-card-head { justify-content: space-between; gap: 14px; }
.node-identity { min-width: 0; gap: 10px; }
.node-icon { display: grid; place-items: center; width: 36px; height: 36px; flex: 0 0 auto; color: #087f72; background: #e8f4f1; border-radius: 6px; }
.node-identity h2 { overflow-wrap: anywhere; color: #263739; font-size: 15px; }
.node-identity code { display: block; max-width: 260px; margin-top: 4px; color: #8a999c; font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.node-state-tags { justify-content: end; flex-wrap: wrap; gap: 6px; }
.node-state-tags :deep(.node-state-pill) { display: inline-flex; align-items: center; gap: 5px; min-height: 26px; padding: 4px 8px; border-width: 1px; font-size: 12px; font-weight: 600; line-height: 16px; }
.node-state-tags :deep(.el-tag__content) { display: inline-flex; align-items: center; gap: 4px; }
.node-address { overflow-wrap: anywhere; color: #65757a; font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; }
.node-facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-top: 1px solid #edf1f0; border-bottom: 1px solid #edf1f0; }
.node-facts > div { display: grid; gap: 6px; min-width: 0; padding: 12px 10px 12px 0; }
.node-facts > div + div { padding-left: 10px; border-left: 1px solid #edf1f0; }
.node-facts span { color: #819093; font-size: 11px; }
.node-facts strong { color: #344447; font-size: 12px; overflow-wrap: anywhere; }
.node-facts strong.admitted { color: #168267; }.node-facts strong.blocked { color: #a66820; }
.node-gauges { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.node-gauge { display: grid; gap: 7px; min-width: 0; }
.gauge-label { display: flex; justify-content: space-between; gap: 8px; color: #748488; font-size: 11px; }.gauge-label span { display: flex; align-items: center; gap: 5px; }.gauge-label strong { color: #354649; font-variant-numeric: tabular-nums; text-align: right; overflow-wrap: anywhere; }
.node-gauges.unavailable .node-gauge:not(:last-child) :deep(.el-progress-bar__outer) { background: #edf1f1; }.node-gauges.unavailable .node-gauge:not(:last-child) :deep(.el-progress-bar__inner) { background: #c6d0d0; }
.node-footer { flex-wrap: wrap; gap: 8px 18px; color: #78878a; font-size: 11px; }.node-rate { gap: 5px; }.node-rate:first-child svg { color: #168267; }.node-rate:nth-child(2) svg { color: #28779b; }.node-rate:nth-child(3) svg { color: #8f6a23; }.node-rate strong { color: #3c4d50; font-variant-numeric: tabular-nums; }.node-sample { margin-left: auto; gap: 5px; color: #94a1a4; }
.node-reasons, .node-no-data { display: flex; align-items: flex-start; gap: 6px; padding: 9px 10px; color: #99511f; background: #fff5e9; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }.node-no-data { color: #78878a; background: #f5f8f8; }
@media (max-width: 1024px) { .node-card-list { grid-template-columns: 1fr; } }
@media (max-width: 700px) { .node-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }.node-summary-item:nth-child(2) { border-right: 0; }.node-summary-item:nth-child(n + 3) { border-top: 1px solid #e7eeec; }.node-summary-item { padding: 15px; }.node-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }.node-facts > div:nth-child(3) { border-left: 0; border-top: 1px solid #edf1f0; }.node-facts > div:nth-child(4) { border-top: 1px solid #edf1f0; }.node-gauges { grid-template-columns: 1fr; gap: 10px; }.node-sample { width: 100%; margin-left: 0; } }
</style>
