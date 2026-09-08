<script setup lang="ts">
// 独立日志工作区：设备选择使用服务端搜索，切换任务时销毁旧订阅和作业视图。
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Search } from "lucide-vue-next";
import { api } from "../../shared/api";
import type { Task } from "../../shared/types";
import LiveLogs from "./LiveLogs.vue";
import LogArchives from "./LogArchives.vue";
import CommandHistory from "../commands/CommandHistory.vue";
const selected = defineModel<string>({ default: "" });
const tasks = ref<Task[]>([]);
const busy = ref(false);
const tab = ref("live");
let generation = 0;
async function search(value = "") {
  const current = ++generation;
  busy.value = true;
  try {
    const result = await api.tasks(1, 100, { search: value || undefined });
    if (current !== generation) return;
    tasks.value = result.items;
    if (selected.value && !tasks.value.some(task => task.id === selected.value)) {
      const task = await api.task(selected.value);
      if (current === generation) tasks.value.unshift(task);
    }
  } catch (error) {
    if (current === generation) ElMessage.error(error instanceof Error ? error.message : "读取设备失败");
  } finally {
    if (current === generation) busy.value = false;
  }
}
watch(selected, () => { tab.value = "live"; });
onMounted(() => void search());
onBeforeUnmount(() => generation++);
</script>
<template>
  <div class="log-workspace-selector">
    <span>采集任务</span>
    <el-select v-model="selected" filterable remote :remote-method="search" :loading="busy" placeholder="选择任务或搜索名称 / IP" aria-label="选择日志任务">
      <el-option v-for="task in tasks" :key="task.id" :label="`${task.name} · ${task.ip}:${task.port}`" :value="task.id" />
    </el-select>
  </div>
  <el-tabs v-if="selected" v-model="tab" class="logs-workspace-tabs">
    <el-tab-pane label="实时打印" name="live"><LiveLogs v-if="tab === 'live'" :key="selected" :task-id="selected" @history="tab = 'archives'" /></el-tab-pane>
    <el-tab-pane label="小时归档与检索" name="archives"><LogArchives v-if="tab === 'archives'" :key="selected" :task-id="selected" /></el-tab-pane>
    <el-tab-pane label="命令记录" name="commands"><CommandHistory v-if="tab === 'commands'" :key="selected" :task-id="selected" /></el-tab-pane>
  </el-tabs>
  <div v-else class="log-workspace-empty"><Search :size="32" /><h2>未选择采集任务</h2></div>
</template>
<style scoped>
.log-workspace-selector { display: flex; align-items: center; gap: 16px; margin: 0 0 22px; color: #76868a; font-size: 13px; }
.log-workspace-selector .el-select { width: min(560px, 100%); }
.log-workspace-selector > span { flex-shrink: 0; }
.log-workspace-empty { display: grid; justify-items: center; gap: 20px; padding: 100px 20px; color: #8b9c9c; border-top: 1px solid #e4e9ea; }
.logs-workspace-tabs { min-width: 0; }
@media(max-width: 700px) { .log-workspace-selector { align-items: stretch; flex-direction: column; gap: 10px; } }
</style>
