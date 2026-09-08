<script setup lang="ts">
// 命令记录只读展示任务的历史执行项，任务切换时重新加载。
import { onBeforeUnmount, ref, watch } from "vue";
import { RefreshCw } from "lucide-vue-next";
import { api } from "../../shared/api";
import type { CommandExecution } from "../../shared/types";
const props = defineProps<{ taskId: string }>();
const items = ref<CommandExecution[]>([]);
const page = ref(1), total = ref(0), loading = ref(false), error = ref("");
const labels: Record<string, string> = { QUEUED: "排队中", SENDING: "发送中", SENT: "已发送", SUCCEEDED: "已完成", FAILED: "失败", CANCELLED: "已取消", UNKNOWN: "结果未知" };
let generation = 0;
async function load() {
  const current = ++generation;
  loading.value = true;
  error.value = "";
  try {
    const data = await api.executions(props.taskId, page.value);
    if (current !== generation) return;
    items.value = data.items;
    total.value = data.total;
  } catch (cause) {
    if (current === generation) error.value = cause instanceof Error ? cause.message : "读取命令记录失败";
  } finally {
    if (current === generation) loading.value = false;
  }
}
// 页码与任务身份变化都使旧请求失效，卸载后不再提交响应。
watch(() => props.taskId, () => { items.value = []; total.value = 0; if (page.value !== 1) page.value = 1; else void load(); }, { immediate: true });
watch(page, () => void load());
onBeforeUnmount(() => { generation++; });
</script>
<template>
  <section class="form-section">
    <div class="section-heading"><h2>命令执行记录</h2><el-tooltip content="刷新命令记录"><el-button :icon="RefreshCw" aria-label="刷新命令记录" @click="load" /></el-tooltip></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-table scrollbar-always-on v-loading="loading" :data="items" class="data-table" empty-text="暂无命令执行记录">
      <el-table-column label="命令 / 配置 ID" min-width="240" show-overflow-tooltip><template #default="{ row }">{{ row.command || row.commandId || row.id }}</template></el-table-column>
      <el-table-column label="来源" width="100"><template #default="{ row }">{{ row.kind === 'MANUAL' ? '手动' : row.kind === 'SCHEDULED' ? '定时' : row.kind || '-' }}</template></el-table-column>
      <el-table-column label="状态" width="120"><template #default="{ row }">{{ labels[row.status] || row.status || '-' }}</template></el-table-column>
      <el-table-column label="时间" min-width="180"><template #default="{ row }">{{ row.createdAt ? new Date(row.createdAt).toLocaleString('zh-CN', { hour12: false }) : '-' }}</template></el-table-column>
      <el-table-column prop="error" label="异常" min-width="200" show-overflow-tooltip />
    </el-table>
    <el-pagination v-model:current-page="page" :page-size="50" :total="total" layout="total, prev, pager, next" />
  </section>
</template>
