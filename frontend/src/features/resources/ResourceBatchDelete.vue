<script setup lang="ts">
// 资源批量删除按选择顺序逐项提交；卸载后停止后续请求，避免切页或登出继续写入。
import { computed, onBeforeUnmount, ref } from "vue";
import { ElMessage } from "element-plus";
import { Trash2 } from "lucide-vue-next";
import { api } from "../../shared/api";
import { runSequentially, type BulkOperationEntry } from "../../shared/bulkOperations";
import { confirmAction } from "../../shared/confirm";
import type { Resource } from "../../shared/types";

const props = defineProps<{ items: Resource[]; disabled?: boolean; selectionGeneration?: number }>();
const emit = defineEmits<{ completed: [entries: BulkOperationEntry[]]; processing: [value: boolean] }>();
const deleting = ref(false);
const results = ref<BulkOperationEntry[]>([]);
let mounted = true;

const failures = computed(() => results.value.filter(item => item.status === "error"));
const taskCount = computed(() => props.items.reduce((total, item) => total + (item.taskCount ?? 0), 0));
const unsettledTaskCount = computed(() => props.items.reduce((total, item) => total + (item.unsettledTaskCount ?? 0), 0));
const selectedNames = computed(() => props.items.map(item => `“${item.name}”`).join("、"));

async function removeSelected() {
  if (deleting.value || props.disabled || !props.items.length) return;
  const current = [...props.items];
  const currentGeneration = props.selectionGeneration;
  const message = `确认删除 ${current.length} 个设备资源：${selectedNames.value}？关联 ${taskCount.value} 个采集任务，其中 ${unsettledTaskCount.value} 个尚未收束。删除已提交后会停止关联采集，已有日志保留，可继续查询和下载。`;
  if (!await confirmAction(message, "确认批量删除设备资源")) return;
  if (!mounted || currentGeneration !== props.selectionGeneration) return;
  deleting.value = true;
  emit("processing", true);
  results.value = await runSequentially(current, async (id, version) => {
    await api.deleteResource(id, version);
  }, { shouldContinue: () => mounted && currentGeneration === props.selectionGeneration });
  deleting.value = false;
  if (!mounted) return;
  emit("processing", false);
  if (currentGeneration !== props.selectionGeneration) return;
  emit("completed", results.value);
  const succeeded = results.value.filter(item => item.status === "success").length;
  if (failures.value.length)
    ElMessage.warning(`已提交删除 ${succeeded} 个资源；${failures.value.length} 个失败，请检查后重试`);
  else ElMessage.success(`已提交删除 ${succeeded} 个资源，已有日志保留`);
}

onBeforeUnmount(() => {
  mounted = false;
  emit("processing", false);
});
</script>

<template>
  <div class="resource-batch-delete">
    <el-button type="danger" :icon="Trash2" :disabled="!items.length || deleting || disabled" :loading="deleting" @click="removeSelected">
      删除已选 {{ items.length ? `(${items.length})` : "" }}
    </el-button>
    <p v-if="results.length" class="bulk-delete-result" aria-live="polite">
      <template v-if="failures.length">删除失败：{{ failures.map(item => `“${item.name}”${item.error ? `（${item.error}）` : ""}`).join("、") }}</template>
      <template v-else>已提交删除 {{ results.length }} 个资源，已有日志保留</template>
    </p>
  </div>
</template>

<style scoped>
.resource-batch-delete { display: flex; align-items: center; gap: 12px; min-width: 0; }
.bulk-delete-result { margin: 0; color: #8b5255; font-size: 12px; overflow-wrap: anywhere; }
@media (max-width: 700px) { .resource-batch-delete { align-items: flex-start; flex-direction: column; gap: 6px; } }
</style>
