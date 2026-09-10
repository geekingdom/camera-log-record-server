<script setup lang="ts">
// 命令记录按实际命令快照展示，定时任务以配置 ID 分组，避免同正文被错误合并。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { RefreshCw } from "lucide-vue-next";
import { api } from "../../shared/api";
import type { CommandExecution, ScheduledCommandProgress } from "../../shared/types";
const props = defineProps<{ taskId: string }>();
const items = ref<CommandExecution[]>([]);
const page = ref(1), total = ref(0), loading = ref(false), error = ref("");
const runId = ref<string | null>(null);
const scheduledCommands = ref<ScheduledCommandProgress[]>([]);
const commandId = ref("");
const labels: Record<string, string> = { QUEUED: "排队中", SENDING: "发送中", SENT: "已发送", SUCCEEDED: "已完成", FAILED: "失败", CANCELLED: "已取消", UNKNOWN: "结果未知" };
let generation = 0;
const selectedScheduledCommand = computed(() => scheduledCommands.value.find(item => item.id === commandId.value));
function commandLabel(item: ScheduledCommandProgress, index: number) {
  return `定时命令 ${index + 1} · ${item.command}`;
}
function executionCommand(row: CommandExecution) {
  return row.command || "历史记录未保存命令内容";
}
function executionIdentifier(row: CommandExecution) {
  const source = row.commandSource === "CURRENT_CONFIGURATION" ? "当前配置推断" : "";
  const identifier = row.commandId ? `配置 ID: ${row.commandId}` : row.id ? `执行 ID: ${row.id}` : "无命令标识";
  return source ? `${source} · ${identifier}` : identifier;
}
async function load() {
  const current = ++generation;
  loading.value = true;
  error.value = "";
  try {
    const data = await api.executions(props.taskId, page.value, commandId.value
      ? { commandId: commandId.value, kind: "SCHEDULED" }
      : undefined);
    if (current !== generation) return;
    items.value = data.items;
    total.value = data.total;
    runId.value = data.runId ?? null;
    scheduledCommands.value = data.scheduledCommands ?? [];
  } catch (cause) {
    if (current === generation) error.value = cause instanceof Error ? cause.message : "读取命令记录失败";
  } finally {
    if (current === generation) loading.value = false;
  }
}
function changeCommand(value: string) {
  commandId.value = value;
  if (page.value !== 1) page.value = 1;
  else void load();
}
// 页码与任务身份变化都使旧请求失效，卸载后不再提交响应。
watch(() => props.taskId, () => {
  items.value = []; total.value = 0; error.value = ""; runId.value = null;
  scheduledCommands.value = []; commandId.value = "";
  if (page.value !== 1) page.value = 1;
  else void load();
}, { immediate: true });
watch(page, () => void load());
onBeforeUnmount(() => { generation++; });
</script>
<template>
  <section class="form-section">
    <div class="section-heading"><div><h2>命令执行记录</h2><p class="command-history-run">{{ runId ? `当前运行：${runId}` : "当前没有运行中的命令预算" }}</p></div><el-tooltip content="刷新命令记录"><el-button :icon="RefreshCw" aria-label="刷新命令记录" @click="load" /></el-tooltip></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <div v-if="scheduledCommands.length" class="command-progress" aria-label="定时命令当前运行预算">
      <button v-for="(item, index) in scheduledCommands" :key="item.id" type="button" class="command-progress-item" :class="{ selected: commandId === item.id }" @click="changeCommand(commandId === item.id ? '' : item.id)">
        <strong>{{ commandLabel(item, index) }}</strong><span>本次运行 {{ item.attempts }} / {{ item.totalExecutions }} 次 · 间隔 {{ item.intervalSeconds }} 秒</span>
      </button>
    </div>
    <div class="command-history-filter"><el-select :model-value="commandId" aria-label="按定时命令筛选执行记录" clearable placeholder="全部命令记录" @update:model-value="changeCommand"><el-option label="全部命令记录" value="" /><el-option v-for="(item, index) in scheduledCommands" :key="item.id" :label="commandLabel(item, index)" :value="item.id" /></el-select><span v-if="selectedScheduledCommand">仅显示该定时命令的执行记录</span></div>
    <el-table scrollbar-always-on v-loading="loading" :data="items" class="data-table" empty-text="暂无命令执行记录">
      <el-table-column label="实际命令" min-width="240" show-overflow-tooltip><template #default="{ row }"><div class="command-execution-command"><span>{{ executionCommand(row) }}</span><small>{{ executionIdentifier(row) }}</small></div></template></el-table-column>
      <el-table-column label="来源" width="100"><template #default="{ row }">{{ row.kind === 'MANUAL' ? '手动' : row.kind === 'SCHEDULED' ? '定时' : row.kind || '-' }}</template></el-table-column>
      <el-table-column label="定时预算" min-width="150"><template #default="{ row }"><span v-if="row.kind === 'SCHEDULED' && row.totalExecutions">第 {{ row.attempts ?? '-' }} / {{ row.totalExecutions }} 次；间隔 {{ row.intervalSeconds }} 秒</span><span v-else>-</span></template></el-table-column>
      <el-table-column label="状态" width="120"><template #default="{ row }">{{ labels[row.status] || row.status || '-' }}</template></el-table-column>
      <el-table-column label="时间" min-width="180"><template #default="{ row }">{{ row.createdAt ? new Date(row.createdAt).toLocaleString('zh-CN', { hour12: false }) : '-' }}</template></el-table-column>
      <el-table-column prop="error" label="异常" min-width="200" show-overflow-tooltip />
    </el-table>
    <el-pagination v-model:current-page="page" :page-size="50" :total="total" layout="total, prev, pager, next" />
  </section>
</template>
<style scoped>
.command-history-run { margin: 3px 0 0; color: #718084; font-size: 12px; }
.command-progress { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 8px; margin: 0 0 12px; }
.command-progress-item { min-width: 0; padding: 10px; border: 1px solid #d9e4e2; border-radius: 4px; background: #fff; color: #304347; text-align: left; cursor: pointer; }
.command-progress-item.selected { border-color: #207e72; background: #edf8f5; }
.command-progress-item strong, .command-progress-item span { display: block; overflow-wrap: anywhere; }
.command-progress-item span, .command-history-filter, .command-execution-command small { color: #718084; font-size: 12px; }
.command-history-filter { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 0 0 12px; }
.command-history-filter .el-select { width: min(100%, 360px); }
.command-execution-command { display: grid; gap: 3px; min-width: 0; }
@media (max-width: 640px) { .command-progress { grid-template-columns: 1fr; } .command-history-filter .el-select { width: 100%; } }
</style>
