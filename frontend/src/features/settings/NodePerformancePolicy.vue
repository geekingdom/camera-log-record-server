<script setup lang="ts">
// 节点性能策略仅编辑持久配置；实际生效值和原因由 Worker 心跳在节点看板呈现。
import type { PerformancePolicy } from "./api";

const props = defineProps<{ policy: PerformancePolicy }>();
const emit = defineEmits<{ "update:policy": [value: PerformancePolicy] }>();

function update(change: Partial<PerformancePolicy>) {
  emit("update:policy", { ...props.policy, ...change });
}
</script>

<template>
  <section class="node-performance-policy" aria-labelledby="node-performance-policy-title">
    <header><h3 id="node-performance-policy-title">采集性能策略</h3><p>已保存策略随 Worker 心跳生效；节点看板报告当前实际生效状态。</p></header>
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
.node-performance-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }.node-performance-grid :deep(.el-input-number) { width: 100%; }
@media (max-width: 560px) { .node-performance-grid { grid-template-columns: 1fr; } }
</style>
