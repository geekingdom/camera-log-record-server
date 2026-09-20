<script setup lang="ts">
// 节点性能策略仅编辑持久配置；实际生效值和原因由 Worker 心跳在节点看板呈现。
import { RefreshCw } from "lucide-vue-next";
import type { PerformancePolicy, PerformanceRecommendation } from "./api";

const props = defineProps<{ policy: PerformancePolicy; recommendation?: PerformanceRecommendation | null; refreshingRecommendation?: boolean }>();
const emit = defineEmits<{ "update:policy": [value: PerformancePolicy]; "apply-recommendation": [value: PerformancePolicy]; "refresh-recommendation": [] }>();

function update(change: Partial<PerformancePolicy>) {
  emit("update:policy", { ...props.policy, ...change });
}

function formatBytes(value?: number | null) {
  if (!Number.isFinite(value) || value! < 0) return "未知";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value!;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount >= 100 || index === 0 ? Math.round(amount) : amount.toFixed(1)} ${units[index]}`;
}

function recommendationSummary(policy: PerformancePolicy) {
  return `${policy.mode === "AUTO" ? "自动" : "手动"}；${policy.bufferMiB} MiB 缓冲；${policy.writeConcurrency} 路写入；${policy.batchKiB} KiB 批量；${policy.flushIntervalMs} ms 最长批等待；${policy.readMiBPerSecond} MiB/s 后台逻辑读取`;
}

function sourceLabel(source: string) {
  return ({ CGROUP_V2: "容器 cgroup v2", CGROUP_V1: "容器 cgroup v1", HOST: "主机", UNKNOWN: "未知" }[source] ?? source);
}

function memoryLimit(value?: number | null) {
  return value === 0 ? "未设置容器上限" : formatBytes(value);
}

function reportedAt(value?: string | null) {
  return value ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "未上报";
}
</script>

<template>
  <section class="node-performance-policy" aria-labelledby="node-performance-policy-title">
    <header><h3 id="node-performance-policy-title">采集性能策略</h3><p>已保存策略随 Worker 心跳生效；节点看板报告当前实际生效状态。</p></header>
    <section class="node-performance-recommendation" aria-label="节点性能推荐">
      <template v-if="recommendation">
        <div class="node-performance-recommendation-head"><strong>节点服务器推荐</strong><div><el-tag size="small" :type="recommendation.available ? 'success' : 'info'" effect="plain">{{ recommendation.available ? "可应用" : "暂不可用" }}</el-tag><el-tooltip content="刷新匹配节点 ID 的最新心跳推荐，不会覆盖当前表单草稿"><el-button text circle :icon="RefreshCw" :loading="refreshingRecommendation" aria-label="刷新节点推荐" @click="emit('refresh-recommendation')" /></el-tooltip></div></div>
        <p>{{ recommendation.reason }}</p>
        <template v-if="recommendation.resources"><span>有效 CPU：{{ recommendation.resources.effectiveCpu }}；可用内存：{{ formatBytes(recommendation.resources.memoryAvailableBytes) }} / 限制 {{ memoryLimit(recommendation.resources.memoryLimitBytes) }}；来源：{{ sourceLabel(recommendation.resources.source) }}</span></template>
        <span>依据心跳：{{ reportedAt(recommendation.reportedAt) }}</span>
        <template v-if="recommendation.policy"><span>{{ recommendationSummary(recommendation.policy) }}</span><el-button type="primary" plain size="small" :disabled="!recommendation.available" @click="emit('apply-recommendation', recommendation.policy)">应用推荐值</el-button></template>
      </template>
      <template v-else><div class="node-performance-recommendation-head"><strong>节点服务器推荐</strong><div><el-tag size="small" type="info" effect="plain">暂不可用</el-tag><el-tooltip content="刷新匹配节点 ID 的最新心跳推荐，不会覆盖当前表单草稿"><el-button text circle :icon="RefreshCw" :loading="refreshingRecommendation" aria-label="刷新节点推荐" @click="emit('refresh-recommendation')" /></el-tooltip></div></div><p>尚未收到匹配节点 ID 的心跳，无法按服务器资源推荐性能值。</p><el-button type="primary" plain size="small" disabled>应用推荐值</el-button></template>
    </section>
    <el-form label-position="top">
      <el-form-item label="策略模式">
        <el-radio-group :key="policy.mode" :model-value="policy.mode" aria-label="采集性能策略模式" @update:model-value="update({ mode: $event as PerformancePolicy['mode'] })">
          <el-radio-button value="AUTO">自动</el-radio-button><el-radio-button value="MANUAL">手动</el-radio-button>
        </el-radio-group>
        <p class="node-help">{{ policy.mode === "AUTO" ? "自动模式仅在以下资源预算上限内自行调节。" : "手动模式使用以下明确参数，仍受节点 CPU 与内存硬资源限制。" }}</p>
      </el-form-item>
      <div class="node-performance-grid">
        <el-form-item :label="policy.mode === 'AUTO' ? '缓冲预算上限（MiB）' : '缓冲容量（MiB）'"><el-input-number :model-value="policy.bufferMiB" :min="16" :max="4096" :precision="0" controls-position="right" aria-label="日志缓冲容量" @update:model-value="update({ bufferMiB: Number($event) })" /></el-form-item>
        <el-form-item :label="policy.mode === 'AUTO' ? '写入并发上限' : '写入并发数'"><el-input-number :model-value="policy.writeConcurrency" :min="1" :max="32" :precision="0" controls-position="right" aria-label="写入并发数" @update:model-value="update({ writeConcurrency: Number($event) })" /></el-form-item>
        <el-form-item :label="policy.mode === 'AUTO' ? '批量上限（KiB）' : '批量大小（KiB）'"><el-input-number :model-value="policy.batchKiB" :min="16" :max="1024" :precision="0" controls-position="right" aria-label="写入批量大小" @update:model-value="update({ batchKiB: Number($event) })" /></el-form-item>
        <el-form-item :label="policy.mode === 'AUTO' ? '最长批等待上限（ms）' : '最长批等待（ms）'"><el-input-number :model-value="policy.flushIntervalMs" :min="10" :max="200" :precision="0" controls-position="right" aria-label="最长批等待" @update:model-value="update({ flushIntervalMs: Number($event) })" /></el-form-item>
        <el-form-item :label="policy.mode === 'AUTO' ? '后台逻辑读取预算上限（MiB/s）' : '后台逻辑读取预算（MiB/s）'"><el-input-number :model-value="policy.readMiBPerSecond" :min="1" :max="200" :precision="0" controls-position="right" aria-label="后台逻辑读取预算" @update:model-value="update({ readMiBPerSecond: Number($event) })" /></el-form-item>
      </div>
    </el-form>
  </section>
</template>

<style scoped>
.node-performance-policy { margin-top: 8px; padding-top: 18px; border-top: 1px solid #e5eaeb; }
.node-performance-policy header { margin-bottom: 12px; }.node-performance-policy h3 { margin: 0 0 5px; color: #354548; font-size: 14px; }.node-performance-policy p { margin: 0; color: #718084; font-size: 12px; line-height: 1.5; }
.node-performance-recommendation { display: grid; gap: 7px; margin-bottom: 14px; padding: 11px 12px; border: 1px solid #dce8e5; background: #f7fbfa; }.node-performance-recommendation-head, .node-performance-recommendation-head > div { display: flex; align-items: center; }.node-performance-recommendation-head { justify-content: space-between; gap: 8px; color: #3b5052; font-size: 12px; }.node-performance-recommendation-head > div { gap: 3px; }.node-performance-recommendation span { color: #687a7d; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }.node-performance-recommendation .el-button[type="primary"] { justify-self: start; }
.node-performance-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }.node-performance-grid :deep(.el-input-number) { width: 100%; }
@media (max-width: 560px) { .node-performance-grid { grid-template-columns: 1fr; } }
</style>
