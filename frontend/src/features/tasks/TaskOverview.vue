<script setup lang="ts">
// 概览严格区分全量计数和当前页快照，避免把分页统计误当作集群指标。
import { computed } from "vue";
import { Layers3, Radio, TriangleAlert, Server } from "lucide-vue-next";
import type { Task } from "../../shared/types";
import { abnormalStatuses } from "./taskStatus";
const props = defineProps<{ tasks: Task[]; total: number; nodes: number }>();
const metrics = computed(() => [
  { label: "任务总数", value: props.total, icon: Layers3, tone: "neutral", scope: "全部任务" },
  { label: "正在采集", value: props.tasks.filter(t => t.status === "COLLECTING").length, icon: Radio, tone: "healthy", scope: "当前页" },
  { label: "异常任务", value: props.tasks.filter(t => abnormalStatuses.has(t.status ?? "")).length, icon: TriangleAlert, tone: "attention", scope: "当前页" },
  { label: "采集节点", value: props.nodes, icon: Server, tone: "neutral", scope: "已发现" },
]);
</script>
<template>
  <section class="task-overview" aria-label="采集概况">
    <div v-for="metric in metrics" :key="metric.label" class="overview-metric" :class="metric.tone">
      <div class="metric-label"><component :is="metric.icon" :size="16" />{{ metric.label }}</div>
      <div class="metric-value"><strong>{{ metric.value.toLocaleString() }}</strong><span>{{ metric.scope }}</span></div>
    </div>
  </section>
</template>
