<script setup lang="ts">
// 片段保留 run/session 与块序号身份；摘要代表归档校验，不能宣称设备断线前后无缺口。
import { FileText } from "lucide-vue-next";
import type { LogFile } from "../../shared/types";
defineProps<{ files: LogFile[] }>();
defineEmits<{ view: [LogFile] }>();
</script>
<template>
  <div class="hour-fragments">
    <div v-for="file in files" :key="file.id" class="fragment-item">
      <div class="fragment-heading"><FileText :size="16" /><strong>{{ file.archiveName || file.rawFileName || file.id }}</strong><el-button :icon="FileText" :disabled="!['OPEN', 'READY'].includes(file.status)" @click="$emit('view', file)">查看内容</el-button></div>
      <dl><dt>节点 / 会话</dt><dd>{{ file.nodeId }} / {{ file.sessionId }}</dd><dt>块序号范围</dt><dd>{{ file.firstSequence ?? '未知' }} 至 {{ file.lastSequence ?? '写入中' }} · {{ file.bytes.toLocaleString() }} 字节</dd><dt>SHA-256</dt><dd>{{ file.sha256 || '尚未生成归档摘要' }}</dd></dl>
    </div>
  </div>
</template>
<style scoped>
.hour-fragments { padding: 16px 24px; background: #f8faf9; }
.fragment-item + .fragment-item { border-top: 1px solid #e0e8e5; padding-top: 16px; margin-top: 16px; }
.fragment-heading { display: flex; align-items: center; gap: 12px; }
.fragment-heading strong { flex: 1; min-width: 0; overflow-wrap: anywhere; font-size: 12px; font-weight: 500; }
dl { display: grid; grid-template-columns: 100px minmax(0, 1fr); gap: 7px; font-size: 11px; color: #708080; }
dd { margin: 0; overflow-wrap: anywhere; }
</style>
