<script setup lang="ts">
// 节点监控只展示真实心跳与资源数据，登记和准入修改由后台配置模块负责。
import { onBeforeUnmount, onMounted, ref } from "vue";
import type { TooltipInstance } from "element-plus";
import type { Node } from "../../shared/types";
defineProps<{ items: Node[]; loading: boolean }>();

const headerLatencyTooltip = ref<TooltipInstance | null>(null);
const rowLatencyTooltips = new Map<string, TooltipInstance>();

// 表格行会随刷新重用 DOM，回调 ref 同步维护当前可关闭的提示层实例。
function setRowLatencyTooltip(nodeId: string, tooltip: unknown) {
  const instance = tooltip as TooltipInstance | null;
  if (instance) rowLatencyTooltips.set(nodeId, instance);
  else rowLatencyTooltips.delete(nodeId);
}

function rowLatencyTooltipRef(nodeId: string) {
  return (tooltip: unknown) => setRowLatencyTooltip(nodeId, tooltip);
}

// 视口变化时主动关闭旧坐标的浮层，保留标签节点，避免宽屏 popper 撑宽窄屏页面。
function closeLatencyTooltips() {
  headerLatencyTooltip.value?.hide();
  rowLatencyTooltips.forEach((tooltip) => tooltip.hide());
}

onMounted(() => window.addEventListener("resize", closeLatencyTooltips));
onBeforeUnmount(() => {
  window.removeEventListener("resize", closeLatencyTooltips);
  rowLatencyTooltips.clear();
});

// 写入延迟取最近批次 P99 与正在写入年龄的较大值，首批尚无样本时仍需暴露等待状态。
function writeLatencyValue(node: Node) {
  return Math.max(Number(node.writeLatencyMs ?? 0), Number(node.writeLatencyPendingMs ?? 0));
}

function writeLatencySamples(node: Node) {
  return Number(node.writeLatencySamples ?? 0);
}

function writeLatencyPending(node: Node) {
  return Number(node.writeLatencyPendingMs ?? 0);
}

function writeLatencyWindow(node: Node) {
  return Number(node.writeLatencyWindowSeconds ?? 60);
}

function formatLatency(value: number) {
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60000) return `${(value / 1000).toFixed(1)} s`;
  return `${(value / 60000).toFixed(1)} min`;
}

// 零样本且没有未完成写入才代表当前没有可展示的延迟指标。
function writeLatencyText(node: Node) {
  if (writeLatencySamples(node) === 0 && writeLatencyPending(node) === 0) return "无样本";
  const value = formatLatency(writeLatencyValue(node));
  return writeLatencySamples(node) === 0 ? `等待写入 ${value}` : value;
}

function hasWriteLatencyWarning(node: Node) {
  return writeLatencyValue(node) > 200;
}

function hasWriteLatencyMeasurement(node: Node) {
  return writeLatencySamples(node) > 0 || writeLatencyPending(node) > 0;
}

function writeLatencyTooltip(node: Node) {
  if (!hasWriteLatencyMeasurement(node)) return `${writeLatencyWindow(node)} 秒窗口内无写入样本。`;
  return `${writeLatencyWindow(node)} 秒窗口内 ${writeLatencySamples(node)} 个完成样本；正在写入年龄已参与取最大值。`;
}
</script>
<template>
  <el-table
    scrollbar-always-on
    v-loading="loading"
    :data="items"
    class="data-table"
    empty-text="暂无节点"
  >
    <el-table-column prop="id" label="节点" min-width="160" /><el-table-column
      prop="url"
      label="地址"
      min-width="190"
    />
    <el-table-column label="状态" width="100"
      ><template #default="{ row }">{{
        Date.now() - new Date(row.heartbeat).getTime() < 30000 ? "在线" : "失联"
      }}</template></el-table-column
    >
    <el-table-column label="任务" width="100"
      ><template #default="{ row }"
        >{{ row.activeTasks }} / {{ row.capacity }}</template
      ></el-table-column
    >
    <el-table-column label="磁盘使用" min-width="130"
      ><template #default="{ row }"
        ><el-progress
          :percentage="Number(Number(row.diskPercent).toFixed(1))"
          :status="row.diskPercent >= 90 ? 'exception' : undefined" /></template
    ></el-table-column>
    <el-table-column label="新任务准入" min-width="130"><template #default="{ row }"><el-tag :type="row.accepting ? 'success' : 'warning'">{{ row.configurationMismatch ? '地址配置不一致' : row.accepting ? '允许接入' : '暂停接入' }}</el-tag></template></el-table-column>
    <el-table-column label="接收速率" width="150"
      ><template #default="{ row }"
        >{{
          (Number(row.inputBytesPerSecond) / 1024).toFixed(1)
        }}
        KiB/s</template
      ></el-table-column
    >
    <el-table-column label="写入延迟" width="140">
      <template #header>
        <el-tooltip
          ref="headerLatencyTooltip"
          content="最慢一路近 60 秒批次 P99 上界与待写时长的较大值；含批次等待，不代表 fsync 或端到端延迟。"
        >
          <span>写入延迟</span>
        </el-tooltip>
      </template>
      <template #default="{ row }">
        <el-tooltip
          :ref="rowLatencyTooltipRef(row.id)"
          :content="writeLatencyTooltip(row)"
        >
          <el-tag
            v-if="hasWriteLatencyMeasurement(row)"
            :type="hasWriteLatencyWarning(row) ? 'warning' : 'info'"
          >
            {{ writeLatencyText(row) }}
          </el-tag>
          <span v-else>无样本</span>
        </el-tooltip>
      </template>
    </el-table-column>
  </el-table>
</template>
