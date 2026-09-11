<script setup lang="ts">
// 紧凑查找栏只负责输入和定位命令，实际匹配与虚拟列表归属调用方。
import { ChevronDown, ChevronUp, X } from "lucide-vue-next";
const value = defineModel<string>({ default: "" });
defineProps<{ current: number; total: number; truncated?: boolean }>();
defineEmits<{ previous: []; next: []; close: [] }>();
</script>

<template>
  <div class="log-find-bar" role="search" aria-label="日志查找">
    <el-input v-model="value" size="small" clearable placeholder="查找日志" aria-label="查找日志" />
    <span class="log-find-count" aria-live="polite">{{ total ? `${current}/${total}${truncated ? "+" : ""}` : "0/0" }}</span>
    <el-tooltip content="上一个匹配"><el-button text size="small" :icon="ChevronUp" aria-label="上一个匹配" :disabled="!total" @click="$emit('previous')" /></el-tooltip>
    <el-tooltip content="下一个匹配"><el-button text size="small" :icon="ChevronDown" aria-label="下一个匹配" :disabled="!total" @click="$emit('next')" /></el-tooltip>
    <el-tooltip content="关闭查找"><el-button text size="small" :icon="X" aria-label="关闭查找" @click="$emit('close')" /></el-tooltip>
  </div>
</template>

<style scoped>
.log-find-bar { display: flex; align-items: center; gap: 4px; min-width: min(340px, 100%); }
.log-find-bar .el-input { flex: 1; min-width: 120px; }
.log-find-count { min-width: 40px; color: #9aa8a3; font: 11px ui-monospace, SFMono-Regular, Menlo, monospace; text-align: right; }
</style>
