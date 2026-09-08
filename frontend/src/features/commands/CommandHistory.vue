<script setup lang="ts">
// 命令记录只读展示任务的历史执行项，任务切换时重新加载。
import { ref, watch } from "vue";
import { api } from "../../shared/api";
import type { CommandExecution } from "../../shared/types";
const props = defineProps<{ taskId: string }>();
const items = ref<CommandExecution[]>([]);
async function load() {
  items.value = (await api.executions(props.taskId)).items;
}
watch(() => props.taskId, load, { immediate: true });
</script>
<template>
  <section class="form-section">
    <h2>命令执行记录</h2>
    <el-table
      :data="items"
      size="small"
      max-height="180"
      empty-text="暂无命令执行记录"
      ><el-table-column prop="command" label="命令" /><el-table-column
        prop="status"
        label="状态"
        width="120" /><el-table-column
        prop="createdAt"
        label="时间"
        width="180"
    /></el-table>
  </section>
</template>
